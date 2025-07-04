import copy
import time
from tqdm import tqdm
import torch
from sklearn.metrics import precision_recall_fscore_support, accuracy_score
import pandas as pd
import numpy as np
import warnings
import torch.nn.utils as torch_utils  # For gradient clipping
import math  # For ceiling function
from torch.utils.data import DataLoader, SubsetRandomSampler  # Import sampler
from src.loss import get_active_criterion  # Import the helper function
from src.regularizers import MaxNorm_via_PGD, Normalizer  # Import regularizers

try:
    import torch_xla.core.xla_model as xm
    _tpu_available = True
except ImportError:
    _tpu_available = False

def train_model(model, dataloaders, criterion, optimizer, scheduler, device,
                num_epochs=25, patience=5, use_amp=True, save_path='best_model.pth',
                log_path='training_log.csv', clip_grad_norm=1.0, train_ratio=1.0,
                criterion_b=None, first_stage_epochs=0, mixup_fn=None,
                max_norm_regularizer=None, tau_normalizer=None, tau_norm_frequency=1):  # Add regularizer parameters
    """
    Trains the model, tracks history, handles early stopping, and saves the best weights.
    
    Additional parameters:
        criterion_b: Secondary criterion to use after first_stage_epochs
        first_stage_epochs: Number of epochs to use criterion_a (primary criterion) before switching
        mixup_fn: MixupCutmixWrapper instance for data augmentation during training
    """
    since = time.time()

    # --- Input Validation ---
    if not isinstance(clip_grad_norm, (float, int)) or clip_grad_norm <= 0:
        print(f"⚠️ Invalid clip_grad_norm value ({clip_grad_norm}). Disabling gradient clipping.")
        clip_grad_norm = None  # Disable clipping if value is invalid

    if not (0.0 < train_ratio <= 1.0):
        warnings.warn(f"⚠️ Invalid train_ratio ({train_ratio}) passed to train_model. Clamping to 1.0.")
        train_ratio = 1.0

    best_model_wts = copy.deepcopy(model.state_dict())
    best_val_metric = 0.0  # Use a generic name, decided by primary metric below
    primary_metric = 'val_acc_macro'  # Metric to monitor for improvement and early stopping
    epochs_no_improve = 0
    nan_inf_counter = 0
    max_nan_inf_tolerance = 5  # Number of NaN/Inf batches tolerated before warning/action

    is_cuda = device.type == 'cuda'
    is_tpu = _tpu_available and 'xla' in str(device)

    # Enable GradScaler only if using CUDA and AMP
    scaler = torch.cuda.amp.GradScaler(enabled=(use_amp and is_cuda))

    history = {
        'epoch': [],
        'train_loss': [], 'train_acc_macro': [], 'train_acc_weighted': [],
        'val_loss': [], 'val_acc_macro': [], 'val_acc_weighted': [],
        'val_precision_macro': [], 'val_recall_macro': [], 'val_f1_macro': [],
        'val_precision_weighted': [], 'val_recall_weighted': [], 'val_f1_weighted': [],
        'lr': []
    }

    # Store original dataloader settings to recreate epoch-specific loader
    original_train_loader = dataloaders.get('train')
    if original_train_loader is None:
        raise ValueError("❌ 'train' dataloader not found in dataloaders dictionary.")

    full_train_dataset = original_train_loader.dataset
    num_total_train_samples = len(full_train_dataset)
    loader_batch_size = original_train_loader.batch_size
    loader_num_workers = original_train_loader.num_workers
    loader_pin_memory = original_train_loader.pin_memory
    loader_collate_fn = original_train_loader.collate_fn
    loader_persistent_workers = getattr(original_train_loader, 'persistent_workers', False)  # Get persistent_workers if available

    print(f"\n🚀 Starting Training Configuration:")
    print(f"   Model: {type(model).__name__}")
    print(f"   Epochs: {num_epochs}, Patience: {patience}")
    print(f"   Device: {device}, AMP: {use_amp}, Grad Clip Norm: {clip_grad_norm}")
    print(f"   Optimizer: {type(optimizer).__name__}, LR Scheduler: {type(scheduler).__name__}")
    
    # Print criterion configuration
    if criterion_b is not None and first_stage_epochs > 0:
        print(f"   Criterion A (first {first_stage_epochs} epochs): {type(criterion).__name__}")
        print(f"   Criterion B (remaining epochs): {type(criterion_b).__name__}")
    else:
        print(f"   Criterion: {type(criterion).__name__}")
        
    print(f"   Best Model Path: {save_path}")
    print(f"   Log Path: {log_path}")
    print(f"   Primary Metric for Improvement: {primary_metric}")
    print(f"   Train Ratio per Epoch: {train_ratio:.2f}")
    print("-" * 30)

    for epoch in range(num_epochs):
        epoch_start_time = time.time()
        print(f'\nEpoch {epoch+1}/{num_epochs}')
        print('-' * 20)
        
        # Log current criterion
        if criterion_b is not None and first_stage_epochs > 0:
            if epoch == first_stage_epochs:
                print(f"🔄 Switching from criterion A to criterion B at epoch {epoch+1}")
            current_criterion = get_active_criterion(epoch, criterion, criterion_b, first_stage_epochs)
            criterion_name = "A" if current_criterion == criterion else "B"
            print(f"   Using criterion {criterion_name}: {type(current_criterion).__name__}")
        else:
            current_criterion = criterion
            
        history['epoch'].append(epoch + 1)
        history['lr'].append(optimizer.param_groups[0]['lr'])  # Log LR at start of epoch

        # --- Create Epoch-Specific Training Loader if ratio < 1.0 ---
        if train_ratio < 1.0:
            num_epoch_samples = math.ceil(num_total_train_samples * train_ratio)
            epoch_indices = torch.randperm(num_total_train_samples)[:num_epoch_samples].tolist()
            epoch_sampler = SubsetRandomSampler(epoch_indices)
            print(f"   Sampling {num_epoch_samples}/{num_total_train_samples} training samples for this epoch.")
            epoch_train_loader = DataLoader(
                full_train_dataset,
                batch_size=loader_batch_size,
                sampler=epoch_sampler,  # Use the sampler
                num_workers=loader_num_workers,
                pin_memory=loader_pin_memory,
                collate_fn=loader_collate_fn,
                persistent_workers=loader_persistent_workers,
                shuffle=False  # Sampler handles shuffling
            )
        else:
            # Use the original loader (assuming it shuffles)
            epoch_train_loader = original_train_loader
            num_epoch_samples = num_total_train_samples  # Use all samples

        # Each epoch has a training and validation phase
        for phase in ['train', 'val']:
            if phase == 'train':
                model.train()  # Set model to training mode
                current_loader = epoch_train_loader  # Use the potentially subsetted loader
                current_dataset_size = num_epoch_samples  # Use the size of the subset for progress bar total
            else:
                model.eval()  # Set model to evaluate mode
                if 'val' not in dataloaders:  # Skip validation if no val loader
                    print("   Skipping validation phase: No validation dataloader provided.")
                    continue
                current_loader = dataloaders['val']
                try:
                    current_dataset_size = len(current_loader.dataset)
                except TypeError:
                    current_dataset_size = 0  # Fallback if dataset has no len

            running_loss = 0.0
            all_preds = []
            all_labels = []
            batch_count = 0
            num_processed_samples = 0  # Initialize counter for processed samples
            phase_start_time = time.time()

            # Use tqdm for progress bar
            # Adjust total based on whether it's train (subset size) or val (full size)
            pbar_total = math.ceil(current_dataset_size / current_loader.batch_size) if current_loader.batch_size > 0 else 0
            pbar = tqdm(current_loader, desc=f'{phase.capitalize()} Epoch {epoch+1}', total=pbar_total, leave=False, unit="batch")

            for inputs, labels in pbar:
                # Skip batch if data loading failed (indicated by empty tensors)
                if inputs.numel() == 0 or labels.numel() == 0:
                    warnings.warn(f"Skipping empty batch in {phase} phase (epoch {epoch+1}). Check data loading.")
                    continue

                inputs = inputs.to(device, non_blocking=True)  # Use non_blocking for potential speedup
                labels = labels.to(device, non_blocking=True)
                
                # Track actual batch size for this iteration
                current_batch_size = inputs.size(0)
                
                # Zero the parameter gradients
                optimizer.zero_grad(set_to_none=True)  # More memory efficient

                # Apply MixUp/CutMix if available and in training phase
                if phase == 'train' and mixup_fn is not None and mixup_fn.is_enabled():
                    # Store original labels before MixUp for metric calculation
                    original_labels = labels.clone()
                    inputs, labels = mixup_fn(inputs, labels)
                else:
                    original_labels = labels

                # Forward pass
                # Track history only in train phase
                with torch.set_grad_enabled(phase == 'train'):
                    # Use autocast for mixed precision if enabled
                    # Determine dtype based on device
                    amp_dtype = torch.float16 if is_cuda else torch.bfloat16
                    with torch.cuda.amp.autocast(enabled=(use_amp and is_cuda), dtype=amp_dtype):
                        outputs = model(inputs)
                        # Use the active criterion for this epoch
                        active_criterion = get_active_criterion(epoch, criterion, criterion_b, first_stage_epochs)
                        loss = active_criterion(outputs, labels)

                    # Check for NaN/Inf loss *before* backward pass
                    if not torch.isfinite(loss).item():
                        nan_inf_counter += 1
                        warnings.warn(f"⚠️ NaN/Inf loss detected in {phase} phase (epoch {epoch+1}, batch {batch_count+1}). Loss: {loss.item()}. Skipping update for this batch.")
                        if nan_inf_counter > max_nan_inf_tolerance:
                            warnings.warn(f"   Exceeded NaN/Inf tolerance ({max_nan_inf_tolerance}). Consider checking model stability, learning rate, or data.")
                        del outputs, loss
                        torch.cuda.empty_cache()  # Try to clear cache if OOM might be related
                        continue  # Skip backprop and metric calculation for this batch
                    
                    # Get predictions for metrics calculation (use original labels even with MixUp)
                    preds = outputs.argmax(dim=1).detach().cpu().numpy()
                    # Use original labels for metric calculation
                    if original_labels.dim() > 1 and original_labels.size(1) > 1:  # If labels are one-hot
                        labels_cpu = original_labels.argmax(dim=1).detach().cpu().numpy()
                    else:  # If labels are standard class indices (1D)
                        labels_cpu = original_labels.detach().cpu().numpy()
                    all_preds.extend(preds)
                    all_labels.extend(labels_cpu)                    # Backward pass + optimize only if in training phase
                    if phase == 'train':
                        if is_cuda and use_amp:
                            scaler.scale(loss).backward()
                            scaler.unscale_(optimizer)
                            if clip_grad_norm is not None:
                                torch_utils.clip_grad_norm_(model.parameters(), clip_grad_norm)
                            scaler.step(optimizer)
                            scaler.update()
                        elif is_tpu:  # Specific handling for TPU
                            loss.backward()
                            if clip_grad_norm is not None:
                                torch_utils.clip_grad_norm_(model.parameters(), clip_grad_norm)
                            xm.optimizer_step(optimizer, barrier=True)  # TPU optimizer step
                        else:  # Standard CPU or CUDA without AMP
                            loss.backward()
                            if clip_grad_norm is not None:
                                torch_utils.clip_grad_norm_(model.parameters(), clip_grad_norm)
                            optimizer.step()
                        
                        # Apply regularizers after optimizer step
                        if max_norm_regularizer is not None:
                            max_norm_regularizer.PGD(model)
                        
                        if tau_normalizer is not None and (epoch + 1) % tau_norm_frequency == 0:
                            tau_normalizer.apply_on(model)# Statistics
                running_loss += loss.item() * current_batch_size
                num_processed_samples += current_batch_size
                batch_count += 1
                pbar.set_postfix({'loss': f'{loss.item():.4f}'})
                
                # Clean up variables
                del outputs, loss
                if 'preds' in locals():
                    del preds
                if 'labels_cpu' in locals():
                    del labels_cpu
                del inputs, labels
                if is_cuda:
                    torch.cuda.empty_cache()

            # --- Epoch End Calculation ---
            phase_duration = time.time() - phase_start_time
            # num_processed_samples is now properly tracked above

            if batch_count == 0 or num_processed_samples == 0:
                print(f"⚠️ No valid batches processed in {phase} phase for epoch {epoch+1} (batch_count={batch_count}, processed_samples={num_processed_samples}). Skipping metrics calculation.")
                history[f'{phase}_loss'].append(float('nan'))
                history[f'{phase}_acc_macro'].append(float('nan'))
                history[f'{phase}_acc_weighted'].append(float('nan'))
                if phase == 'val':
                    history['val_precision_macro'].append(float('nan'))
                    history['val_recall_macro'].append(float('nan'))
                    history['val_f1_macro'].append(float('nan'))
                    history['val_precision_weighted'].append(float('nan'))
                    history['val_recall_weighted'].append(float('nan'))
                    history['val_f1_weighted'].append(float('nan'))
                continue

            # Calculate loss based on processed samples
            epoch_loss = running_loss / num_processed_samples
            # print(f'{all_labels}, {all_preds}')  # Debugging line to check labels and predictions
            # Calculate metrics based on processed samples
            if len(all_preds) > 0 and len(all_labels) > 0:
                precision_macro, recall_macro, f1_macro, _ = precision_recall_fscore_support(
                    all_labels, all_preds, average='macro', zero_division=0
                )
                precision_weighted, recall_weighted, f1_weighted, _ = precision_recall_fscore_support(
                    all_labels, all_preds, average='weighted', zero_division=0
                )
                epoch_acc_macro = accuracy_score(all_labels, all_preds)
                epoch_acc_macro_avg_per_class = recall_macro
                epoch_acc_weighted = accuracy_score(all_labels, all_preds)
            else:
                print(f"⚠️ No predictions generated for {phase} phase in epoch {epoch+1}. Setting metrics to 0.")
                epoch_acc_macro_avg_per_class, epoch_acc_weighted = 0.0, 0.0
                precision_macro, recall_macro, f1_macro = 0.0, 0.0, 0.0
                precision_weighted, recall_weighted, f1_weighted = 0.0, 0.0, 0.0

            # Update history
            history[f'{phase}_loss'].append(epoch_loss)
            history[f'{phase}_acc_macro'].append(epoch_acc_macro_avg_per_class)
            history[f'{phase}_acc_weighted'].append(epoch_acc_weighted)

            if phase == 'val':
                history['val_precision_macro'].append(precision_macro)
                history['val_recall_macro'].append(recall_macro)
                history['val_f1_macro'].append(f1_macro)
                history['val_precision_weighted'].append(precision_weighted)
                history['val_recall_weighted'].append(recall_weighted)
                history['val_f1_weighted'].append(f1_weighted)

            # Print results
            print(f'{phase.capitalize():<5} Loss: {epoch_loss:.4f} | Acc (Mac): {epoch_acc_macro_avg_per_class:.4f} | Acc (Wgt): {epoch_acc_weighted:.4f} | Time: {phase_duration:.2f}s')
            if phase == 'val':
                print(f'      P(Mac): {precision_macro:.4f} | R(Mac): {recall_macro:.4f} | F1(Mac): {f1_macro:.4f}')
                print(f'      P(Wgt): {precision_weighted:.4f} | R(Wgt): {recall_weighted:.4f} | F1(Wgt): {f1_weighted:.4f}')

            # --- Validation Phase Specific Logic ---
            if phase == 'val':
                # --- Correctly select the metric value based on primary_metric ---
                if primary_metric == 'val_acc_macro':
                    current_val_metric = epoch_acc_macro_avg_per_class  # Use average recall for macro accuracy
                elif primary_metric == 'val_acc_weighted':
                    current_val_metric = epoch_acc_weighted
                elif primary_metric == 'val_f1_macro':
                    current_val_metric = f1_macro
                elif primary_metric == 'val_f1_weighted':
                    current_val_metric = f1_weighted
                # Add other metrics like precision/recall if needed
                else:
                    # Default or fallback if primary_metric is misconfigured
                    warnings.warn(f"Unrecognized primary_metric '{primary_metric}'. Defaulting to 'val_acc_macro'.")
                    current_val_metric = epoch_acc_macro_avg_per_class

                # --- Scheduler Step ---
                if isinstance(scheduler, torch.optim.lr_scheduler.ReduceLROnPlateau):
                    scheduler.step(current_val_metric)
                elif scheduler is not None:
                    # Step other schedulers like StepLR, CosineAnnealingLR
                    scheduler.step()

                current_lr = optimizer.param_groups[0]['lr']
                print(f"   LR after scheduler step: {current_lr:.6f}")

                # --- Check for Improvement and Save Best Model ---
                if current_val_metric > best_val_metric:
                    print(f'✅ {primary_metric} improved ({best_val_metric:.4f} --> {current_val_metric:.4f}). Saving model to {save_path}')
                    best_val_metric = current_val_metric
                    # Save the model state dict
                    model_to_save = model.module if isinstance(model, torch.nn.DataParallel) else model
                    if is_tpu:
                        # Save for TPU using xm.save
                        xm.save(model_to_save.state_dict(), save_path)
                    else:
                        # Standard PyTorch save
                        torch.save(model_to_save.state_dict(), save_path)
                    epochs_no_improve = 0
                    if mixup_fn is not None:
                        mixup_fn.enabled = True  # Re-enable MixUp/CutMix if it was disabled
                else:
                    epochs_no_improve += 1
                    print(f'📉 {primary_metric} did not improve for {epochs_no_improve} epoch(s). Best: {best_val_metric:.4f}')
                    if epochs_no_improve == patience - 5:
                        if mixup_fn is not None:
                            print(f'📊 {epochs_no_improve} epochs without improvement. Disabling MixUp/CutMix to stabilize training.')
                            mixup_fn.enabled = False  # Disable MixUp/CutMix if no improvement for 5 epochs

        epoch_duration = time.time() - epoch_start_time
        print(f"Epoch {epoch+1} duration: {epoch_duration:.2f}s")

        if epochs_no_improve >= patience:
            print(f'\n⏰ Early stopping triggered after {epoch+1} epochs ({patience} epochs without improvement on {primary_metric}).')
            break

    time_elapsed = time.time() - since
    print(f'\n🏁 Training complete in {time_elapsed//60:.0f}m {time_elapsed%60:.0f}s')
    print(f'🏆 Best {primary_metric}: {best_val_metric:.4f} (achieved after epoch {epoch + 1 - epochs_no_improve})')

    try:
        max_len = max(len(v) for v in history.values())
        for k, v in history.items():
            if len(v) < max_len:
                padding_val = float('nan') if 'loss' in k or 'acc' in k or 'f1' in k or 'precision' in k or 'recall' in k else -1
                history[k].extend([padding_val] * (max_len - len(v)))

        history_df = pd.DataFrame(history)
        history_df.set_index('epoch', inplace=True)
        history_df.to_csv(log_path, index=True, float_format='%.6f')
        print(f"💾 Training log saved to {log_path}")
    except Exception as e:
        print(f"⚠️ Could not save training log to {log_path}: {e}")
        print("   History data:", history)

    print(f"🔄 Loading best model weights from {save_path} into model...")
    try:
        best_wts_loaded = torch.load(save_path, map_location=device)
        if isinstance(model, torch.nn.DataParallel):
            model.module.load_state_dict(best_wts_loaded)
        else:
            model.load_state_dict(best_wts_loaded)
        print("   Best weights loaded successfully.")
    except FileNotFoundError:
        print(f"❌ Error: Best model file not found at {save_path}. Returning model with last epoch weights.")
    except Exception as e:
        print(f"❌ Error loading best model weights: {e}. Returning model with last epoch weights.")

    return model, history, best_val_metric

def train_classifier_only(model, dataloaders, criterion, optimizer, scheduler, device,
                          num_epochs=25, patience=5, use_amp=True, save_path='best_classifier_model.pth',
                          log_path='classifier_training_log.csv', clip_grad_norm=1.0, train_ratio=1.0,
                          criterion_b=None, first_stage_epochs=0, init_best_val_metric=0.0,
                          mixup_fn=None,  # Add MixUp support
                          max_norm_regularizer=None, tau_normalizer=None, tau_norm_frequency=1):  # Add regularizer parameters
    """
    Trains ONLY THE CLASSIFIER part of the model, assuming feature extractor layers are frozen.
    Tracks history, handles early stopping, and saves the best weights for the classifier.

    IMPORTANT:
    - Before calling this function, ensure that the feature extractor layers of the 'model'
      have their 'requires_grad' attribute set to False.
    - The 'optimizer' should be initialized ONLY with the parameters of the classifier
      (i.e., parameters for which 'requires_grad' is True).

    Additional parameters:
        criterion_b: Secondary criterion to use after first_stage_epochs
        first_stage_epochs: Number of epochs to use criterion_a (primary criterion) before switching
        mixup_fn: MixupCutmixWrapper instance for data augmentation during training

    Example of model preparation before calling:
    ```python
    # Assuming model has 'feature_extractor' and 'classifier' attributes
    # (e.g., model.feature_extractor, model.classifier)
    # Freeze feature extractor layers
    # for param in model.feature_extractor.parameters():
    #     param.requires_grad = False
    # Ensure classifier layers are trainable
    # for param in model.classifier.parameters():
    #     param.requires_grad = True

    # Optimizer for classifier parameters only
    # trainable_params = filter(lambda p: p.requires_grad, model.parameters())
    # optimizer = torch.optim.Adam(trainable_params, lr=0.001)
    # Or, more explicitly if you know the classifier attribute:
    # optimizer = torch.optim.Adam(model.classifier.parameters(), lr=0.001)

    # Then call:
    # train_classifier_only(model, dataloaders, criterion, optimizer, scheduler, device, ...)
    ```
    
    Parameters are similar to train_model.
    """
    since = time.time()

    # --- Input Validation ---
    if not isinstance(clip_grad_norm, (float, int)) or clip_grad_norm <= 0:
        print(f"⚠️ Invalid clip_grad_norm value ({clip_grad_norm}). Disabling gradient clipping.")
        clip_grad_norm = None  # Disable clipping if value is invalid

    if not (0.0 < train_ratio <= 1.0):
        warnings.warn(f"⚠️ Invalid train_ratio ({train_ratio}) passed to train_classifier_only. Clamping to 1.0.")
        train_ratio = 1.0

    best_model_wts = copy.deepcopy(model.state_dict())
    best_val_metric = init_best_val_metric  # Use a generic name, decided by primary metric below
    primary_metric = 'val_acc_macro'  # Metric to monitor for improvement and early stopping
    epochs_no_improve = 0
    nan_inf_counter = 0
    max_nan_inf_tolerance = 5  # Number of NaN/Inf batches tolerated before warning/action

    is_cuda = device.type == 'cuda'
    is_tpu = _tpu_available and 'xla' in str(device)

    # Enable GradScaler only if using CUDA and AMP
    scaler = torch.cuda.amp.GradScaler(enabled=(use_amp and is_cuda))

    history = {
        'epoch': [],
        'train_loss': [], 'train_acc_macro': [], 'train_acc_weighted': [],
        'val_loss': [], 'val_acc_macro': [], 'val_acc_weighted': [],
        'val_precision_macro': [], 'val_recall_macro': [], 'val_f1_macro': [],
        'val_precision_weighted': [], 'val_recall_weighted': [], 'val_f1_weighted': [],
        'lr': []
    }

    original_train_loader = dataloaders.get('train')
    if original_train_loader is None:
        raise ValueError("❌ 'train' dataloader not found in dataloaders dictionary.")

    full_train_dataset = original_train_loader.dataset
    num_total_train_samples = len(full_train_dataset)
    loader_batch_size = original_train_loader.batch_size
    loader_num_workers = original_train_loader.num_workers
    loader_pin_memory = original_train_loader.pin_memory
    loader_collate_fn = original_train_loader.collate_fn
    loader_persistent_workers = getattr(original_train_loader, 'persistent_workers', False)

    print(f"\n🚀 Starting Classifier-Only Training Configuration:")
    print(f"   Model: {type(model).__name__} (Expecting Feature Extractor Frozen)")
    num_total_params = sum(p.numel() for p in model.parameters())
    num_trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"   Trainable Parameters: {num_trainable_params}/{num_total_params}")
    if num_trainable_params == 0:
        warnings.warn("⚠️ CRITICAL: No trainable parameters found in the model. Ensure classifier parameters have 'requires_grad=True' and the optimizer is correctly configured for them. Training will likely have no effect.")
    elif num_trainable_params == num_total_params and num_total_params > 0:
        warnings.warn("⚠️ All model parameters appear to be trainable. This function is intended for classifier-only training. Please ensure the feature extractor part of your model is frozen (params.requires_grad = False).")
    elif num_trainable_params < num_total_params:
        print(f"   Looks good: {num_trainable_params} parameters are trainable (classifier) out of {num_total_params} total.")


    print(f"   Epochs: {num_epochs}, Patience: {patience}")
    print(f"   Device: {device}, AMP: {use_amp}, Grad Clip Norm: {clip_grad_norm}")
    print(f"   Optimizer: {type(optimizer).__name__}, LR Scheduler: {type(scheduler).__name__}")
    
    # Print criterion configuration
    if criterion_b is not None and first_stage_epochs > 0:
        print(f"   Criterion A (first {first_stage_epochs} epochs): {type(criterion).__name__}")
        print(f"   Criterion B (remaining epochs): {type(criterion_b).__name__}")
    else:
        print(f"   Criterion: {type(criterion).__name__}")
        
    # Print MixUp configuration
    if mixup_fn is not None:
        print(f"   MixUp/CutMix: {'Enabled' if mixup_fn.is_enabled() else 'Disabled'}")
        if mixup_fn.is_enabled():
            print(f"   MixUp α: {mixup_fn.mixup_alpha}, CutMix α: {mixup_fn.cutmix_alpha}")
    else:
        print(f"   MixUp/CutMix: Not provided")
        
    print(f"   Best Classifier Model Path: {save_path}")
    print(f"   Classifier Log Path: {log_path}")
    print(f"   Primary Metric for Improvement: {primary_metric}")
    print(f"   Train Ratio per Epoch: {train_ratio:.2f}")
    print("-" * 30)

    for epoch in range(num_epochs):
        epoch_start_time = time.time()
        print(f'\nEpoch {epoch+1}/{num_epochs} (Classifier Training)')
        print('-' * 20)
        
        if criterion_b is not None and first_stage_epochs > 0:
            if epoch == first_stage_epochs:
                print(f"🔄 Switching from criterion A to criterion B at epoch {epoch+1}")
            current_criterion = get_active_criterion(epoch, criterion, criterion_b, first_stage_epochs)
            criterion_name = "A" if current_criterion == criterion else "B"
            print(f"   Using criterion {criterion_name}: {type(current_criterion).__name__}")
        else:
            current_criterion = criterion
            
        history['epoch'].append(epoch + 1)
        history['lr'].append(optimizer.param_groups[0]['lr'])

        if train_ratio < 1.0:
            num_epoch_samples = math.ceil(num_total_train_samples * train_ratio)
            epoch_indices = torch.randperm(num_total_train_samples)[:num_epoch_samples].tolist()
            epoch_sampler = SubsetRandomSampler(epoch_indices)
            print(f"   Sampling {num_epoch_samples}/{num_total_train_samples} training samples for this epoch.")
            epoch_train_loader = DataLoader(
                full_train_dataset,
                batch_size=loader_batch_size,
                sampler=epoch_sampler,
                num_workers=loader_num_workers,
                pin_memory=loader_pin_memory,
                collate_fn=loader_collate_fn,
                persistent_workers=loader_persistent_workers,
                shuffle=False
            )
        else:
            epoch_train_loader = original_train_loader
            num_epoch_samples = num_total_train_samples

        for phase in ['train', 'val']:
            if phase == 'train':
                model.train()
                current_loader = epoch_train_loader
                current_dataset_size = num_epoch_samples
            else:
                model.eval()
                if 'val' not in dataloaders:
                    print("   Skipping validation phase: No validation dataloader provided.")
                    continue
                current_loader = dataloaders['val']
                try:
                    current_dataset_size = len(current_loader.dataset)
                except TypeError:
                    current_dataset_size = 0

            running_loss = 0.0
            all_preds = []
            all_labels = []
            batch_count = 0
            num_processed_samples = 0  # Initialize counter for processed samples
            phase_start_time = time.time()

            pbar_total = math.ceil(current_dataset_size / current_loader.batch_size) if current_loader.batch_size > 0 else 0
            pbar_desc = f'{phase.capitalize()} Epoch {epoch+1} (Classifier)'
            pbar = tqdm(current_loader, desc=pbar_desc, total=pbar_total, leave=False, unit="batch")

            for inputs, labels in pbar:
                if inputs.numel() == 0 or labels.numel() == 0:
                    warnings.warn(f"Skipping empty batch in {phase} phase (epoch {epoch+1}). Check data loading.")
                    continue

                inputs = inputs.to(device, non_blocking=True)
                labels = labels.to(device, non_blocking=True)
                
                # Track actual batch size for this iteration
                current_batch_size = inputs.size(0)

                optimizer.zero_grad(set_to_none=True)

                # Apply MixUp/CutMix if available and in training phase
                if phase == 'train' and mixup_fn is not None and mixup_fn.is_enabled():
                    # Store original labels before MixUp for metric calculation
                    original_labels = labels.clone()
                    inputs, labels = mixup_fn(inputs, labels)
                else:
                    original_labels = labels

                with torch.set_grad_enabled(phase == 'train'):
                    amp_dtype = torch.float16 if is_cuda else torch.bfloat16
                    with torch.cuda.amp.autocast(enabled=(use_amp and is_cuda), dtype=amp_dtype):
                        outputs = model(inputs)
                        active_criterion = get_active_criterion(epoch, criterion, criterion_b, first_stage_epochs)
                        loss = active_criterion(outputs, labels)

                    if not torch.isfinite(loss).item():
                        nan_inf_counter += 1
                        warnings.warn(f"⚠️ NaN/Inf loss detected in {phase} phase (epoch {epoch+1}, batch {batch_count+1}). Loss: {loss.item()}. Skipping update for this batch.")
                        if nan_inf_counter > max_nan_inf_tolerance:                        
                            warnings.warn(f"   Exceeded NaN/Inf tolerance ({max_nan_inf_tolerance}). Consider checking model stability, learning rate, or data.")
                        del outputs, loss
                        if is_cuda: torch.cuda.empty_cache()
                        continue

                    # Get predictions for metrics calculation (use original labels even with MixUp)
                    preds = outputs.argmax(dim=1).detach().cpu().numpy()
                    # Use original labels for metric calculation
                    if original_labels.dim() > 1 and original_labels.size(1) > 1:  # If labels are one-hot
                        labels_cpu = original_labels.argmax(dim=1).detach().cpu().numpy()
                    else:  # If labels are standard class indices (1D)
                        labels_cpu = original_labels.detach().cpu().numpy()
                    all_preds.extend(preds)
                    all_labels.extend(labels_cpu)

                    if phase == 'train':
                        if is_cuda and use_amp:
                            scaler.scale(loss).backward()
                            scaler.unscale_(optimizer)
                            if clip_grad_norm is not None:
                                torch_utils.clip_grad_norm_(model.parameters(), clip_grad_norm)
                            scaler.step(optimizer)
                            scaler.update()
                        elif is_tpu:
                            loss.backward()
                            if clip_grad_norm is not None:
                                torch_utils.clip_grad_norm_(model.parameters(), clip_grad_norm)
                            xm.optimizer_step(optimizer, barrier=True)
                        else:
                            loss.backward()
                            if clip_grad_norm is not None:
                                torch_utils.clip_grad_norm_(model.parameters(), clip_grad_norm)
                            optimizer.step()
                        
                        # Apply regularizers after optimizer step
                        if max_norm_regularizer is not None:
                            max_norm_regularizer.PGD(model)
                        
                        if tau_normalizer is not None and (epoch + 1) % tau_norm_frequency == 0:
                            tau_normalizer.apply_on(model)

                # Statistics - update counters after successful processing
                running_loss += loss.item() * current_batch_size
                num_processed_samples += current_batch_size
                batch_count += 1
                pbar.set_postfix({'loss': f'{loss.item():.4f}'})
                
                # Clean up variables
                del outputs, loss
                if 'preds' in locals():
                    del preds
                if 'labels_cpu' in locals():
                    del labels_cpu
                del inputs, labels
                if is_cuda:
                    torch.cuda.empty_cache()

            phase_duration = time.time() - phase_start_time
            # num_processed_samples is now properly tracked above

            if batch_count == 0 or num_processed_samples == 0:
                print(f"⚠️ No valid batches processed in {phase} phase for epoch {epoch+1}. Skipping metrics.")
                history[f'{phase}_loss'].append(float('nan'))
                history[f'{phase}_acc_macro'].append(float('nan'))
                history[f'{phase}_acc_weighted'].append(float('nan'))
                if phase == 'val':
                    for metric_key in ['precision_macro', 'recall_macro', 'f1_macro', 'precision_weighted', 'recall_weighted', 'f1_weighted']:
                        history[f'val_{metric_key}'].append(float('nan'))
                continue

            epoch_loss = running_loss / num_processed_samples

            if len(all_preds) > 0 and len(all_labels) > 0:
                precision_macro, recall_macro, f1_macro, _ = precision_recall_fscore_support(
                    all_labels, all_preds, average='macro', zero_division=0
                )
                precision_weighted, recall_weighted, f1_weighted, _ = precision_recall_fscore_support(
                    all_labels, all_preds, average='weighted', zero_division=0
                )
                epoch_acc_macro_avg_per_class = recall_macro # Using recall_macro as proxy for balanced accuracy
                epoch_acc_weighted = accuracy_score(all_labels, all_preds)
            else:
                epoch_acc_macro_avg_per_class, epoch_acc_weighted = 0.0, 0.0
                precision_macro, recall_macro, f1_macro = 0.0, 0.0, 0.0
                precision_weighted, recall_weighted, f1_weighted = 0.0, 0.0, 0.0

            history[f'{phase}_loss'].append(epoch_loss)
            history[f'{phase}_acc_macro'].append(epoch_acc_macro_avg_per_class)
            history[f'{phase}_acc_weighted'].append(epoch_acc_weighted)

            if phase == 'val':
                history['val_precision_macro'].append(precision_macro)
                history['val_recall_macro'].append(recall_macro)
                history['val_f1_macro'].append(f1_macro)
                history['val_precision_weighted'].append(precision_weighted)
                history['val_recall_weighted'].append(recall_weighted)
                history['val_f1_weighted'].append(f1_weighted)

            print(f'{phase.capitalize():<5} Loss: {epoch_loss:.4f} | Acc (Mac): {epoch_acc_macro_avg_per_class:.4f} | Acc (Wgt): {epoch_acc_weighted:.4f} | Time: {phase_duration:.2f}s')
            if phase == 'val':
                print(f'      P(Mac): {precision_macro:.4f} | R(Mac): {recall_macro:.4f} | F1(Mac): {f1_macro:.4f}')
                print(f'      P(Wgt): {precision_weighted:.4f} | R(Wgt): {recall_weighted:.4f} | F1(Wgt): {f1_weighted:.4f}')

            if phase == 'val':
                current_val_metric_map = {
                    'val_acc_macro': epoch_acc_macro_avg_per_class,
                    'val_acc_weighted': epoch_acc_weighted,
                    'val_f1_macro': f1_macro,
                    'val_f1_weighted': f1_weighted
                }
                current_val_metric = current_val_metric_map.get(primary_metric, epoch_acc_macro_avg_per_class)
                if primary_metric not in current_val_metric_map:
                     warnings.warn(f"Unrecognized primary_metric '{primary_metric}'. Defaulting to 'val_acc_macro'.")


                if isinstance(scheduler, torch.optim.lr_scheduler.ReduceLROnPlateau):
                    scheduler.step(current_val_metric)
                elif scheduler is not None:
                    scheduler.step()
                
                current_lr = optimizer.param_groups[0]['lr']
                print(f"   LR after scheduler step: {current_lr:.6f}")

                if current_val_metric > best_val_metric:
                    print(f'✅ {primary_metric} improved ({best_val_metric:.4f} --> {current_val_metric:.4f}). Saving classifier model to {save_path}')
                    best_val_metric = current_val_metric
                    model_to_save = model.module if isinstance(model, torch.nn.DataParallel) else model
                    if is_tpu:
                        xm.save(model_to_save.state_dict(), save_path)
                    else:
                        torch.save(model_to_save.state_dict(), save_path)
                    epochs_no_improve = 0
                    if mixup_fn is not None:
                        mixup_fn.enabled = True  # Re-enable MixUp/CutMix if it was disabled
                else:
                    epochs_no_improve += 1
                    print(f'📉 {primary_metric} did not improve for {epochs_no_improve} epoch(s). Best: {best_val_metric:.4f}')
                    if epochs_no_improve == patience - 5:
                        if mixup_fn is not None:
                            print(f'📊 {epochs_no_improve} epochs without improvement. Disabling MixUp/CutMix to stabilize training.')
                            mixup_fn.enabled = False  # Disable MixUp/CutMix if no improvement for 5 epochs

        epoch_duration = time.time() - epoch_start_time
        print(f"Epoch {epoch+1} duration: {epoch_duration:.2f}s")

        if epochs_no_improve >= patience:
            print(f'\n⏰ Early stopping triggered after {epoch+1} epochs ({patience} epochs without improvement on {primary_metric}).')
            break

    time_elapsed = time.time() - since
    print(f'\n🏁 Classifier Training complete in {time_elapsed//60:.0f}m {time_elapsed%60:.0f}s')
    print(f'🏆 Best {primary_metric}: {best_val_metric:.4f} (achieved after epoch {epoch + 1 - epochs_no_improve if epoch + 1 - epochs_no_improve > 0 else 1})')

    try:
        max_len = max(len(v) for v in history.values())
        for k, v in history.items():
            if len(v) < max_len:
                padding_val = float('nan') if any(m in k for m in ['loss', 'acc', 'f1', 'precision', 'recall']) else -1
                history[k].extend([padding_val] * (max_len - len(v)))

        history_df = pd.DataFrame(history)
        history_df.set_index('epoch', inplace=True)
        history_df.to_csv(log_path, index=True, float_format='%.6f')
        print(f"💾 Classifier training log saved to {log_path}")
    except Exception as e:
        print(f"⚠️ Could not save classifier training log to {log_path}: {e}")
        print("   History data:", history)

    print(f"🔄 Loading best classifier model weights from {save_path} into model...")
    try:
        best_wts_loaded = torch.load(save_path, map_location=device)
        if isinstance(model, torch.nn.DataParallel):
            model.module.load_state_dict(best_wts_loaded)
        else:
            model.load_state_dict(best_wts_loaded)
        print("   Best classifier weights loaded successfully.")
    except FileNotFoundError:
        print(f"❌ Error: Best classifier model file not found at {save_path}. Returning model with last epoch weights.")
    except Exception as e:
        print(f"❌ Error loading best classifier model weights: {e}. Returning model with last epoch weights.")

    return model, history
