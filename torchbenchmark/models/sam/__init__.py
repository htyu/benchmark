# Copyright (c) Meta Platforms, Inc. and affiliates.
# This software may be used and distributed according to the terms of the GNU General Public License version 3.

from ...util.model import BenchmarkModel
from .build_sam import sam_model_registry
from .predictor import SamPredictor
from PIL import Image
import numpy as np
import cv2
from torchbenchmark.tasks import COMPUTER_VISION
import torch
import os


    
class Model(BenchmarkModel):
    task = COMPUTER_VISION.SEGMENTATION
    DEFAULT_EVAL_BSIZE = 32
    
    def __init__(self, test, device, jit=False, batch_size=1, extra_args=[]):
        super().__init__(test=test, device=device, jit=jit, batch_size=batch_size, extra_args=extra_args)
        
        # Checkpoint options are here https://github.com/facebookresearch/segment-anything#model-checkpoints
        sam_checkpoint = os.path.join('.data', 'sam_vit_h_4b8939.pth')
        model_type = "vit_h"

        # TODO Before merge: Add the real checkpoint when done testing
        self.model = sam_model_registry[model_type](checkpoint=sam_checkpoint)
        self.model.to(device=device)

        # TODO Before merge: Make the batch size configurable
        # We don't actually pass in a tensor but pass in an image
        # self.example_inputs = [{0 : torch.randn(3, 224, 224).to(device=device)}], 
        
   
    def get_module(self):
        return self.model # self.example_inputs
    
    def train(self):
        error_msg = """
            As of May 17, 2023
            Some base VIT checkpoints are available for SAM but getting the dataset
            requires a research license. It's easy to make up a training loop on random
            data and if that's interesting please let @msaroufim know
            https://github.com/facebookresearch/segment-anything#dataset
        """
        return NotImplementedError(error_msg)

    def eval(self):
        predictor = SamPredictor(self.model)
        image_path = os.path.join('.data', 'truck.jpg')
        image = cv2.imread(image_path)
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        predictor.set_image(image)

        input_point = np.array([[500, 375]])
        input_label = np.array([1])
        masks, scores, logits = predictor.predict(
        point_coords=input_point,
        point_labels=input_label,
        multimask_output=True)
        return (masks,)