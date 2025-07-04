import os
import argparse
import yaml
import torch
import torch.nn.functional as F
import numpy as np
import matplotlib.pyplot as plt
from PIL import Image
import cv2
import traceback

# Import from existing modules
from src.model_initializer import initialize_model
from src.device_handler import get_device


def setup_gradcam_hooks(model, target_layer=None):
    """Setup GradCAM using hook-based approach"""
    model.eval()
    
    if target_layer is None:
        target_layer = find_target_layer(model)
    
    print(f"✅ Setting up GradCAM hooks on: {target_layer.__class__.__name__}")
    
    gradcam_data = {'features': None, 'gradients': None}
    
    def forward_hook(module, input, output):
        if isinstance(output, torch.Tensor):
            gradcam_data['features'] = output
        elif isinstance(output, (list, tuple)) and len(output) > 0:
            gradcam_data['features'] = output[0]
    
    def backward_hook(module, grad_input, grad_output):
        if isinstance(grad_output, tuple) and len(grad_output) > 0:
            gradcam_data['gradients'] = grad_output[0]
    
    forward_handle = target_layer.register_forward_hook(forward_hook)
    backward_handle = target_layer.register_backward_hook(backward_hook)
    
    gradcam_data['forward_handle'] = forward_handle
    gradcam_data['backward_handle'] = backward_handle
    
    return gradcam_data


def find_target_layer(model):
    """Find suitable target layer for GradCAM"""
    print(f"🔍 Finding target layer for GradCAM...")
    
    all_modules = list(model.named_modules())
    print(f"   Found {len(all_modules)} total modules")
    
    target_layer = None
    
    # 1. Check for timm model structures with 'blocks'
    if hasattr(model, 'blocks') and isinstance(model.blocks, (torch.nn.Sequential, torch.nn.ModuleList)) and len(model.blocks) > 0:
        last_block_candidate = model.blocks[-1]
        if hasattr(last_block_candidate, 'norm1'):
            target_layer = last_block_candidate.norm1
            print(f"🎯 Selected last block's 'norm1': {type(target_layer).__name__}")
        elif hasattr(last_block_candidate, 'norm'):
            target_layer = last_block_candidate.norm
            print(f"🎯 Selected last block's 'norm': {type(target_layer).__name__}")
        else:
            target_layer = last_block_candidate
            print(f"🎯 Selected last block: {type(target_layer).__name__}")
    
    # 2. Check for ResNet-like 'layer4'
    elif hasattr(model, 'layer4') and isinstance(model.layer4, torch.nn.Sequential) and len(model.layer4) > 0:
        target_layer = model.layer4[-1]
        print(f"🎯 Selected last module in 'layer4': {type(target_layer).__name__}")
    
    # 3. Check for features
    elif hasattr(model, 'features'):
        if hasattr(model.features, 'norm5'):
            target_layer = model.features.norm5
            print(f"🎯 Selected 'features.norm5': {type(target_layer).__name__}")
        elif isinstance(model.features, torch.nn.Sequential) and len(model.features) > 0:
            candidate = model.features[-1]
            if not isinstance(candidate, (torch.nn.AdaptiveAvgPool2d, torch.nn.AvgPool2d, torch.nn.MaxPool2d)):
                target_layer = candidate
                print(f"🎯 Selected last module in 'features': {type(target_layer).__name__}")
    
    # 4. Check for conv_head
    if target_layer is None and hasattr(model, 'conv_head'):
        target_layer = model.conv_head
        print(f"🎯 Selected 'conv_head': {type(target_layer).__name__}")
    
    # 5. Fallback: Find last Conv2d layer
    if target_layer is None:
        print("   Searching for last Conv2d layer...")
        for name, module in model.named_modules():
            if isinstance(module, torch.nn.Conv2d):
                target_layer = module
                print(f"🎯 Selected last Conv2d '{name}': {type(target_layer).__name__}")
    
    if target_layer is None:
        raise ValueError("Could not find suitable target layer for GradCAM")
    
    return target_layer


def compute_gradcam_hooks(model, gradcam_data, input_tensor, target_class=None):
    """Compute GradCAM using hook-based approach"""
    try:
        if input_tensor.dim() == 3:
            input_tensor = input_tensor.unsqueeze(0)
        
        print(f"🔍 Input shape: {input_tensor.shape}, Target class: {target_class}")
        
        # Ensure gradients are enabled
        input_tensor.requires_grad_(True)
        
        # Forward pass
        output = model(input_tensor)
        
        if target_class is None:
            target_class = output.argmax(dim=1).item()
        
        print(f"📊 Model output shape: {output.shape}, predicted class: {target_class}")
        print(f"📊 Class probabilities: {F.softmax(output, dim=1)[0].detach().cpu().numpy()}")
        
        # Backward pass
        model.zero_grad()
        one_hot = torch.zeros_like(output)
        one_hot[0, target_class] = 1.0
        output.backward(gradient=one_hot, retain_graph=True)
        
        # Get captured data
        gradients = gradcam_data['gradients']
        features = gradcam_data['features']
        
        if gradients is None or features is None:
            print("❌ Failed to capture gradients or features from hooks")
            return None
        
        print(f"📐 Features shape: {features.shape}, Gradients shape: {gradients.shape}")
        
        # Use first sample in batch
        gradients = gradients[0]  # (C, H, W)
        features = features[0]    # (C, H, W)
        
        # Global Average Pooling on gradients
        if gradients.ndim == 3:  # Spatial (C, H, W)
            weights = torch.mean(gradients, dim=(1, 2))  # (C,)
        elif gradients.ndim == 2:  # Sequence (C, D)
            weights = torch.mean(gradients, dim=1)  # (C,)
        else:
            print(f"❌ Unexpected gradient dimensions: {gradients.ndim}")
            return None
        weights = F.relu(weights)  # Apply ReLU to weights
        # Weighted sum of feature maps
        cam = torch.zeros(features.shape[1:], dtype=torch.float32, device=features.device)
        for i, w in enumerate(weights):
            if i < features.shape[0]:
                cam += w * features[i]
        
        # Apply ReLU
        cam = F.relu(cam)
        
        # Check if CAM is all zeros after ReLU
        if torch.all(cam == 0):
            print("⚠️ WARNING: CAM is all zeros after ReLU!")
            return None
        
        # Normalize CAM to [0, 1]
        cam_min = torch.min(cam)
        cam_max = torch.max(cam)
        if cam_max > cam_min:
            cam = (cam - cam_min) / (cam_max - cam_min + 1e-8)
        else:
            cam = torch.zeros_like(cam)
        
        # Convert to numpy and resize
        cam_np = cam.detach().cpu().numpy()
        
        if cam_np.ndim == 2:  # Spatial CAM
            target_size = (input_tensor.shape[2], input_tensor.shape[3])
            try:
                cam_resized = cv2.resize(cam_np, (target_size[1], target_size[0]))
                print(f"✅ CAM resized to {cam_resized.shape}")
                return cam_resized
            except Exception as e:
                print(f"❌ Error resizing CAM: {e}")
                return cam_np
        else:
            print(f"⚠️ CAM is not 2D (shape: {cam_np.shape}), returning as-is")
            return cam_np
            
    except Exception as e:
        print(f"❌ Error in GradCAM computation: {e}")
        traceback.print_exc()
        return None


def cleanup_gradcam_hooks(gradcam_data):
    """Clean up GradCAM hooks"""
    try:
        if 'forward_handle' in gradcam_data:
            gradcam_data['forward_handle'].remove()
        if 'backward_handle' in gradcam_data:
            gradcam_data['backward_handle'].remove()
        print("🧹 GradCAM hooks cleaned up")
    except Exception as e:
        print(f"⚠️ Error cleaning up hooks: {e}")


def load_model_from_checkpoint(model_path, model_name, num_classes, device):
    """Load a trained model from checkpoint"""
    print(f"🔄 Loading model checkpoint from: {model_path}")
    
    # Initialize model architecture
    model, input_size, transform, model_config = initialize_model(
        model_name, 
        num_classes=num_classes, 
        use_pretrained=False,  # We'll load our trained weights
        feature_extract=False
    )
    
    # Load trained weights
    try:
        checkpoint = torch.load(model_path, map_location=device)
        if isinstance(checkpoint, dict) and 'model_state_dict' in checkpoint:
            model.load_state_dict(checkpoint['model_state_dict'])
            print(f"✅ Loaded model state from checkpoint (with metadata)")
        else:
            model.load_state_dict(checkpoint)
            print(f"✅ Loaded model state directly")
        
        model.to(device)
        model.eval()  # Ensure model is in evaluation mode
        print(f"📍 Model moved to {device} and set to eval mode")
        
    except Exception as e:
        print(f"❌ Error loading checkpoint: {e}")
        raise e
    
    return model, input_size, transform, model_config


def extract_normalization_from_transform(transform):
    """Extract normalization parameters from transform pipeline"""
    normalize_mean = None
    normalize_std = None
    
    if hasattr(transform, 'transforms'):
        # Look through transform pipeline for Normalize
        for t in transform.transforms:
            if hasattr(t, 'mean') and hasattr(t, 'std'):
                normalize_mean = np.array(t.mean)
                normalize_std = np.array(t.std)
                print(f"🎯 Found normalization: mean={normalize_mean}, std={normalize_std}")
                break
    elif hasattr(transform, 'mean') and hasattr(transform, 'std'):
        # Direct normalization transform
        normalize_mean = np.array(transform.mean)
        normalize_std = np.array(transform.std)
        print(f"🎯 Found direct normalization: mean={normalize_mean}, std={normalize_std}")
    
    if normalize_mean is None or normalize_std is None:
        # Fallback to ImageNet defaults
        normalize_mean = np.array([0.5, 0.5, 0.5])
        normalize_std = np.array([0.5, 0.5, 0.5])
        print(f"⚠️ No normalization found in transform, using ImageNet defaults")
    
    return normalize_mean, normalize_std


def load_and_preprocess_image(image_path, transform):
    """Load and preprocess a single image"""
    print(f"📸 Loading image: {image_path}")
    
    try:
        # Load image
        image = Image.open(image_path).convert('RGB')
        original_size = image.size
        print(f"✅ Loaded image with size: {original_size}")
        
        # Apply transform
        if transform:
            image_tensor = transform(image)
            print(f"🔄 Applied transform, tensor shape: {image_tensor.shape}")
            
            # Extract and store normalization parameters for later use
            normalize_mean, normalize_std = extract_normalization_from_transform(transform)
            
            return image, image_tensor, normalize_mean, normalize_std
        else:
            # Basic transform if none provided
            from torchvision import transforms
            basic_transform = transforms.Compose([
                transforms.Resize((224, 224)),
                transforms.ToTensor(),
                transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
            ])
            image_tensor = basic_transform(image)
            print(f"🔄 Applied basic transform, tensor shape: {image_tensor.shape}")
            
            return image, image_tensor, np.array([0.485, 0.456, 0.406]), np.array([0.229, 0.224, 0.225])
        
    except Exception as e:
        print(f"❌ Error loading image: {e}")
        raise e


def tensor_to_rgb_image(tensor, transform=None, normalize_mean=None, normalize_std=None):
    """Convert tensor to RGB image for visualization with proper denormalization"""
    # Move to CPU and convert to numpy
    if tensor.dim() == 4:
        tensor = tensor.squeeze(0)  # Remove batch dimension
    
    image = tensor.cpu().numpy()
    
    # Transpose from CHW to HWC
    if image.shape[0] == 3:  # If channels first
        image = np.transpose(image, (1, 2, 0))
    
    # Extract normalization from transform if provided
    if transform is not None and normalize_mean is None and normalize_std is None:
        normalize_mean, normalize_std = extract_normalization_from_transform(transform)
    elif normalize_mean is None or normalize_std is None:
        # Fallback values
        normalize_mean = np.array([0.5, 0.5, 0.5])
        normalize_std = np.array([0.5, 0.5, 0.5])
        print(f"📊 Using fallback ImageNet normalization")
    
    print(f"📊 Denormalizing with mean={normalize_mean}, std={normalize_std}")
    
    # Apply denormalization: x = (x_norm * std) + mean
    image = image * normalize_std + normalize_mean
    
    # Clip to [0, 1] range for proper display
    image = np.clip(image, 0, 1)
    
    print(f"📐 Denormalized image shape: {image.shape}, range: [{image.min():.3f}, {image.max():.3f}]")
    
    return image


def create_gradcam_visualization(rgb_img, cam, class_names, predicted_class, confidence, save_path=None, 
                               transform=None, normalize_mean=None, normalize_std=None):
    """Create GradCAM visualization with proper denormalization from transform"""
    try:
        # Ensure RGB image is properly denormalized using actual transform values
        if isinstance(rgb_img, torch.Tensor):
            rgb_img = tensor_to_rgb_image(rgb_img, transform, normalize_mean, normalize_std)
        
        # Ensure RGB image is in proper range [0, 1]
        if rgb_img.max() > 1.0:
            rgb_img = rgb_img / 255.0
        rgb_img = np.clip(rgb_img, 0, 1)
        
        # Ensure cam is in proper range [0, 1]
        cam = np.clip(cam, 0, 1)
    
        # Apply slight gaussian smoothing to CAM for better visualization
        try:
            from scipy.ndimage import gaussian_filter
            cam_smooth = gaussian_filter(cam, sigma=1.0)
        except ImportError:
            print("⚠️ scipy not available, using original CAM without smoothing")
            cam_smooth = cam
        
        # Create the CAM visualization using opencv
        try:
            from pytorch_grad_cam.utils.image import show_cam_on_image
            visualization = show_cam_on_image(rgb_img, cam_smooth, use_rgb=True, colormap=cv2.COLORMAP_JET)
        except ImportError:
            # Fallback implementation if pytorch_grad_cam not available
            print("⚠️ pytorch_grad_cam not available, using manual overlay")
            # Convert CAM to RGB heatmap
            cam_colored = cv2.applyColorMap(np.uint8(255 * cam_smooth), cv2.COLORMAP_JET)
            cam_colored = cv2.cvtColor(cam_colored, cv2.COLOR_BGR2RGB) / 255.0
            # Blend with original image
            visualization = 0.6 * rgb_img + 0.4 * cam_colored
            visualization = np.clip(visualization, 0, 1)
        
        # Create figure with larger size for better visibility
        fig, axes = plt.subplots(1, 3, figsize=(18, 6))
        
        # Original image
        axes[0].imshow(rgb_img)
        axes[0].set_title("Original Image", fontsize=12, fontweight='bold')
        axes[0].axis('off')
        
        # Heatmap
        im = axes[1].imshow(cam_smooth, cmap='jet', vmin=0, vmax=1, interpolation='bilinear')
        axes[1].set_title("GradCAM Heatmap", fontsize=12, fontweight='bold')
        axes[1].axis('off')
        
        # Add colorbar for heatmap
        cbar = plt.colorbar(im, ax=axes[1], fraction=0.046, pad=0.04)
        cbar.set_label('Activation Intensity', rotation=270, labelpad=15)
        
        # Overlay
        axes[2].imshow(visualization)
        axes[2].set_title("GradCAM Overlay", fontsize=12, fontweight='bold')
        axes[2].axis('off')
        
        # Add prediction info to the title
        class_name = class_names[predicted_class] if class_names and predicted_class < len(class_names) else f"Class {predicted_class}"
        axes[2].set_title(f"GradCAM Overlay\nPredicted: {class_name} ({confidence:.2%})", 
                         fontsize=12, fontweight='bold')
        axes[2].axis('off')
        
        # Add overall title
        fig.suptitle(f"GradCAM Analysis - {class_name}", fontsize=16, fontweight='bold')
        
        # Adjust layout
        plt.tight_layout()
        
        # Save if path provided
        if save_path:
            plt.savefig(save_path, dpi=300, bbox_inches='tight')
            print(f"💾 Visualization saved to: {save_path}")
        
        plt.show()
        
        return fig, visualization
            
    except Exception as e:
        print(f"❌ Error creating visualization: {e}")
        traceback.print_exc()
        return None, None


# Update the main inference function to pass normalization parameters
def run_inference_with_gradcam(model_path, image_path, model_name, num_classes, class_names=None, save_path=None):
    """Run inference with GradCAM visualization"""
    try:
        # Setup device
        device = get_device()
        
        # Load model
        model, input_size, transform, model_config = load_model_from_checkpoint(
            model_path, model_name, num_classes, device
        )
        
        # Load and preprocess image - now returns normalization parameters
        original_image, image_tensor, normalize_mean, normalize_std = load_and_preprocess_image(image_path, transform)
        
        # Setup GradCAM
        gradcam_data = setup_gradcam_hooks(model)
        
        try:
            # Run inference and GradCAM
            image_tensor = image_tensor.to(device)
            cam = compute_gradcam_hooks(model, gradcam_data, image_tensor)
            
            if cam is not None:
                # Get prediction
                with torch.no_grad():
                    output = model(image_tensor.unsqueeze(0))
                    probabilities = F.softmax(output, dim=1)
                    predicted_class = output.argmax(dim=1).item()
                    confidence = probabilities[0, predicted_class].item()
                
                # Create visualization with actual normalization parameters
                fig, visualization = create_gradcam_visualization(
                    image_tensor, cam, class_names, predicted_class, confidence, 
                    save_path, transform, normalize_mean, normalize_std
                )
                
                return fig, visualization, predicted_class, confidence
            else:
                print("❌ Failed to generate GradCAM")
                return None, None, None, None
                
        finally:
            cleanup_gradcam_hooks(gradcam_data)
            
    except Exception as e:
        print(f"❌ Error in inference: {e}")
        traceback.print_exc()
        return None, None, None, None


def main():
    parser = argparse.ArgumentParser(description='Single Image Inference with GradCAM')
    parser.add_argument('--model_name', type=str, required=True,
                        help='Timm model name (e.g., mobilenetv4_hybrid_medium.ix_e550_r384_in1k)')
    parser.add_argument('--checkpoint', type=str, required=True,
                        help='Path to model checkpoint (.pth file)')
    parser.add_argument('--image_path', type=str, required=True,
                        help='Path to input image')
    parser.add_argument('--config_path', type=str, default='config_local.yaml',
                        help='Path to config file (default: config_local.yaml)')
    parser.add_argument('--output_dir', type=str, default='./gradcam_output',
                        help='Output directory for results (default: ./gradcam_output)')
    parser.add_argument('--device', type=str, default='auto',
                        help='Device to use: auto, cuda, cpu (default: auto)')
    
    args = parser.parse_args()
    
    print(f"🚀 Single Image Inference with GradCAM")
    print(f"   Model: {args.model_name}")
    print(f"   Checkpoint: {args.checkpoint}")
    print(f"   Image: {args.image_path}")
    print(f"   Output: {args.output_dir}")
    
    # Check if files exist
    if not os.path.exists(args.checkpoint):
        print(f"❌ Checkpoint not found: {args.checkpoint}")
        return
    
    if not os.path.exists(args.image_path):
        print(f"❌ Image not found: {args.image_path}")
        return
    
    # Load configuration for class names
    class_names = []
    num_classes = 1000  # Default for ImageNet
    
    if os.path.exists(args.config_path):
        try:
            with open(args.config_path, 'r') as file:
                config = yaml.safe_load(file)
            
            class_names = config.get('class_names', [])
            
            # Apply class remapping if enabled
            remapping_config = config.get('class_remapping', {})
            if remapping_config.get('enabled', False):
                final_class_names = remapping_config.get('final_class_names', [])
                if final_class_names:
                    class_names = final_class_names
                    print(f"📋 Using remapped class names: {len(class_names)} classes")
            
            if class_names:
                num_classes = len(class_names)
                print(f"📋 Loaded {num_classes} class names from config")
            else:
                print(f"⚠️ No class names found in config, using default")
                
        except Exception as e:
            print(f"⚠️ Error loading config: {e}, using defaults")
    else:
        print(f"⚠️ Config file not found: {args.config_path}, using defaults")
    
    # Setup device
    if args.device == 'auto':
        device, _ = get_device(True)
    elif args.device == 'cuda':
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    else:
        device = torch.device('cpu')
    
    print(f"🖥️ Using device: {device}")
    
    # Create output directory
    os.makedirs(args.output_dir, exist_ok=True)
    
    try:
        # Load model
        model, input_size, transform, model_config = load_model_from_checkpoint(
            args.checkpoint, args.model_name, num_classes, device
        )
        
        # Load and preprocess image
        original_image, image_tensor, normalize_mean, normalize_std = load_and_preprocess_image(args.image_path, transform)
        
        # Move to device
        input_tensor = image_tensor.unsqueeze(0).to(device)
        
        # Perform inference
        print(f"\n🔮 Performing inference...")
        with torch.no_grad():
            outputs = model(input_tensor)
            probabilities = F.softmax(outputs, dim=1)
            confidence, predicted_class = torch.max(probabilities, 1)
            
            predicted_class = predicted_class.item()
            confidence = confidence.item()
            
            if predicted_class < len(class_names):
                class_name = class_names[predicted_class]
            else:
                class_name = f"Class_{predicted_class}"
            
            print(f"✅ Prediction: {class_name} (Class {predicted_class})")
            print(f"📊 Confidence: {confidence:.4f}")
            
            # Show top-5 predictions if available
            top5_probs, top5_classes = torch.topk(probabilities, min(5, probabilities.size(1)))
            print(f"📊 Top-5 predictions:")
            for i, (prob, cls) in enumerate(zip(top5_probs[0], top5_classes[0])):
                cls_idx = cls.item()
                if cls_idx < len(class_names):
                    cls_name = class_names[cls_idx]
                else:
                    cls_name = f"Class_{cls_idx}"
                print(f"   {i+1}. {cls_name}: {prob.item():.4f}")
        
        # Setup and compute GradCAM
        print(f"\n🔥 Generating GradCAM...")
        gradcam_data = setup_gradcam_hooks(model)
        
        try:
            # Compute GradCAM for predicted class
            cam = compute_gradcam_hooks(model, gradcam_data, input_tensor, target_class=predicted_class)
            
            if cam is not None:
                # Convert tensor to RGB for visualization
                rgb_img = tensor_to_rgb_image(image_tensor)
                
                # Create output filename
                # Extract image name after '/test/' if present, else use basename
                image_path_lower = args.image_path.replace("\\", "/").lower()
                if "/test/" in image_path_lower:
                    image_name = args.image_path.replace("\\", "/").split("/test/", 1)[-1]
                    image_name = os.path.splitext(image_name)[0].replace("/", "_")
                else:
                    image_name = os.path.splitext(os.path.basename(args.image_path))[0]
                output_filename = f"{image_name}_{args.checkpoint.split('/')[-1].replace('.pth', '')}_gradcam.png"
                output_path = os.path.join(args.output_dir, output_filename)
                
                # Create visualization
                create_gradcam_visualization(
                    rgb_img, cam, class_names, predicted_class, confidence, output_path, transform, normalize_mean, normalize_std
                )
                
                print(f"✅ GradCAM visualization saved: {output_path}")
            else:
                print(f"❌ Failed to generate GradCAM")
        
        finally:
            # Always clean up hooks
            cleanup_gradcam_hooks(gradcam_data)
        
        print(f"\n🎉 Analysis complete!")
        print(f"📁 Results saved in: {args.output_dir}")
        
    except Exception as e:
        print(f"❌ Error during inference: {e}")
        traceback.print_exc()


if __name__ == "__main__":
    main()
