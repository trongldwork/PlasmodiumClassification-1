import numpy as np
import torch
import torch.nn as nn
import math

# The classes below wrap core functions to impose weight regurlarization constraints in training or finetuning a network.
            
class MaxNorm_via_PGD():
    # learning a max-norm constrainted network via projected gradient descent (PGD) 
    def __init__(self, thresh=0.1, LpNorm=2, tau = 1):
        self.thresh = thresh
        self.LpNorm = LpNorm
        self.tau = tau
        self.perLayerThresh = []
        
    def setPerLayerThresh(self, model):
        # set per-layer thresholds
        self.perLayerThresh = []
        
        for curLayer in [model.get_classifier()]: #here we only apply MaxNorm over the last 
            print(curLayer)
            curparam = curLayer.weight.data
            if len(curparam.shape)<=1: 
                self.perLayerThresh.append(float('inf'))
                continue
            curparam_vec = curparam.reshape((curparam.shape[0], -1))
            neuronNorm_curparam = torch.linalg.norm(curparam_vec, ord=self.LpNorm, dim=1).detach().unsqueeze(-1)
            curLayerThresh = neuronNorm_curparam.min() + self.thresh*(neuronNorm_curparam.max() - neuronNorm_curparam.min())
            self.perLayerThresh.append(curLayerThresh)
                
    def PGD(self, model):
        if len(self.perLayerThresh)==0:
            self.setPerLayerThresh(model)
        
        for i, curLayer in enumerate([model.get_classifier()]): #here we only apply MaxNorm over the last 
            curparam = curLayer.weight.data


            curparam_vec = curparam.reshape((curparam.shape[0], -1))
            neuronNorm_curparam = (torch.linalg.norm(curparam_vec, ord=self.LpNorm, dim=1)**self.tau).detach().unsqueeze(-1)
            scalingVect = torch.ones_like(curparam)    
            curLayerThresh = self.perLayerThresh[i]
            
            idx = neuronNorm_curparam > curLayerThresh
            idx = idx.squeeze()
            tmp = curLayerThresh / (neuronNorm_curparam[idx].squeeze())**(self.tau)
            for _ in range(len(scalingVect.shape)-1):
                tmp = tmp.unsqueeze(-1)

            scalingVect[idx] = torch.mul(scalingVect[idx], tmp)
            curparam[idx] = scalingVect[idx] * curparam[idx] 

class Normalizer(): 
    def __init__(self, LpNorm=2, tau = 1):
        self.LpNorm = LpNorm
        self.tau = tau
  
    def apply_on(self, model): #this method applies tau-normalization on the classifier layer

        for curLayer in [model.get_classifier()]: #change to last layer: Done
            curparam = curLayer.weight.data

            curparam_vec = curparam.reshape((curparam.shape[0], -1))
            neuronNorm_curparam = (torch.linalg.norm(curparam_vec, ord=self.LpNorm, dim=1)**self.tau).detach().unsqueeze(-1)
            scalingVect = torch.ones_like(curparam)    
            
            idx = neuronNorm_curparam == neuronNorm_curparam
            idx = idx.squeeze()
            tmp = 1 / (neuronNorm_curparam[idx].squeeze())
            for _ in range(len(scalingVect.shape)-1):
                tmp = tmp.unsqueeze(-1)

            scalingVect[idx] = torch.mul(scalingVect[idx], tmp)
            curparam[idx] = scalingVect[idx] * curparam[idx]

if __name__ == "__main__":
    import timm

    # Load a model from timm
    model_name = "mobilenetv3_large_100.miil_in21k_ft_in1k"
    num_classes = 7
    model = timm.create_model(model_name, pretrained=False, num_classes=num_classes)
    print("Model loaded from timm.")

    # Print classifier layer before regularization
    classifier = model.get_classifier()
    print("Classifier weights before MaxNorm PGD:")
    print(classifier.weight.data)

    # Apply MaxNorm regularizer
    maxnorm = MaxNorm_via_PGD(thresh=0.1, LpNorm=2, tau=1)
    maxnorm.setPerLayerThresh(model)
    maxnorm.PGD(model)
    print("Classifier weights after MaxNorm PGD:")
    print(classifier.weight)

    # Apply Normalizer
    normalizer = Normalizer(LpNorm=2, tau=1)
    normalizer.apply_on(model)
    print("Classifier weights after Normalizer:")
    print(classifier.weight)
