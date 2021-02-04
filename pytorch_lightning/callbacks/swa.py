# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Stochastic Weight Averaging Callback
====================================

"""
from copy import deepcopy
from typing import Callable, Optional, Union

import torch
from torch import nn

import pytorch_lightning as pl
from pytorch_lightning.callbacks import Callback
from pytorch_lightning.utilities import _PYTORCH_GREATER_EQUAL_1_6_0, rank_zero_warn
from pytorch_lightning.utilities.exceptions import MisconfigurationException

if _PYTORCH_GREATER_EQUAL_1_6_0:
    from torch.optim.swa_utils import SWALR


class StochasticWeightAveraging(Callback):

    def __init__(
        self,
        swa_epoch_start: Union[int, float] = 0.8,
        swa_lrs: Optional[Union[float, list]] = None,
        annealing_epochs: int = 10,
        annealing_strategy: str = "cos",
        avg_fn: Optional[Callable] = None,
        device: Optional[Union[torch.device, str]] = torch.device("cpu"),
    ):

        r"""

        Implements the Stochastic Weight Averaging (SWA) Callback to average a model.

        Stochastic Weight Averaging was proposed in ``Averaging Weights Leads to
        Wider Optima and Better Generalization`` by Pavel Izmailov, Dmitrii
        Podoprikhin, Timur Garipov, Dmitry Vetrov and Andrew Gordon Wilson
        (UAI 2018).

        This documentation is highly inspired by PyTorch's work on swa
        and this callback exposes the same arguments as PyTorch's ``swa_utils`` function.

        Find ``swa_utils` source code there: https://github.com/pytorch/pytorch/blob/v1.7.1/torch/optim/swa_utils.py
        Find ``SWA explanation`` there: https://pytorch.org/blog/pytorch-1.6-now-includes-stochastic-weight-averaging/

        .. note:: `StochasticWeightAveraging` is currently not supported for multiple optimizers / schedulers.

        Arguments:

            swa_epoch_start (int, float): If provided as int, the procedure will start from
                the ``swa_epoch_start``-th epoch. If provided as float between 0 and 1,
                the procedure will start from ``int(swa_epoch_start * max_epochs)`` epoch

            swa_lrs (float or list): the learning rate value for all param groups
                together or separately for each group.

            annealing_epochs (int): number of epochs in the annealing phase
                (default: 10)

            annealing_strategy (str): "cos" or "linear"; specifies the annealing
                strategy: "cos" for cosine annealing, "linear" for linear annealing
                (default: "cos")

            avg_fn (function, optional): the averaging function used to update
                parameters; the function must take in the current value of the
                :class:`AveragedModel` parameter, the current value of :attr:`model`
                parameter and the number of models already averaged; if None,
                equally weighted average is used (default: None)

            device (torch.device, optional): if provided, the averaged model will be
                stored on the `device`. Default: `cpu`
                When None is provided, it will infer the `device` from ``pl_module``.

        """

        err_msg = "swa_epoch_start should be a >0 integer or a float between 0 and 1."
        if isinstance(swa_epoch_start, int) and swa_epoch_start < 1:
            raise MisconfigurationException(err_msg)
        if isinstance(swa_epoch_start, float) and not (0 <= swa_epoch_start <= 1):
            raise MisconfigurationException(err_msg)

        if not isinstance(swa_lrs, (float, list)) \
           or isinstance(swa_lrs, float) and swa_lrs <= 0 \
           or isinstance(swa_lrs, list) and not all(lr > 0 and isinstance(lr, float) for lr in swa_lrs):
            raise MisconfigurationException("swa_lrs should be a positive float or a list of positive float.")

        if avg_fn is not None and not isinstance(avg_fn, Callable):
            raise MisconfigurationException("avg_fn should be callable.")

        if device is not None and not isinstance(device, (torch.device, str)):
            raise MisconfigurationException(f"device is expected to be a torch.device or a str. Found {device}")

        self._swa_epoch_start = swa_epoch_start
        self._swa_lrs = swa_lrs
        self._annealing_epochs = annealing_epochs
        self._annealing_strategy = annealing_strategy
        self._avg_fn = avg_fn or self.avg_fn
        self._device = device
        self._model_contains_batch_norm = None

    @property
    def swa_start(self) -> int:
        return max(self._swa_epoch_start - 1, 0)  # 0-based

    @property
    def swa_end(self) -> int:
        return self._max_epochs - 1  # 0-based

    @staticmethod
    def pl_module_contains_batch_norm(pl_module):
        return any(isinstance(module, nn.modules.batchnorm._BatchNorm) for module in pl_module.modules())

    def reset_batch_norm_and_save_state(self, average_model):
        """
        Credit to PyTorch Team.
        Adapted from https://github.com/pytorch/pytorch/blob/v1.7.1/torch/optim/swa_utils.py#L115
        """
        self.momenta = {}
        for module in average_model.modules():
            if isinstance(module, nn.modules.batchnorm._BatchNorm):
                module.running_mean = torch.zeros_like(
                    module.running_mean, device=average_model.device, dtype=module.running_mean.dtype)
                module.running_var = torch.ones_like(
                    module.running_var, device=average_model.device, dtype=module.running_var.dtype)
                self.momenta[module] = module.momentum
                module.momentum = None
                module.num_batches_tracked *= 0

    def reset_momenta(self):
        """
        Credit to PyTorch Team.
        Taken from https://github.com/pytorch/pytorch/blob/v1.7.1/torch/optim/swa_utils.py#L164
        """
        for bn_module in self.momenta.keys():
            bn_module.momentum = self.momenta[bn_module]

    def on_before_accelerator_backend_setup(self, trainer, pl_module):
        # copy the model before moving it to accelerator device.
        self._average_model = deepcopy(pl_module)
        optimizers = trainer.optimizers
        lr_schedulers = trainer.lr_schedulers

        if len(optimizers) > 1:
            raise MisconfigurationException("SWA currently not supported for more than 1 optimizer.")

        if len(lr_schedulers) > 1:
            raise MisconfigurationException("SWA currently not supported for more than 1 lr_scheduler.")

        if isinstance(self._swa_epoch_start, float):
            self._swa_epoch_start = int(trainer.max_epochs * self._swa_epoch_start)

        self._model_contains_batch_norm = self.pl_module_contains_batch_norm(pl_module)

        self._max_epochs = trainer.max_epochs
        if self._model_contains_batch_norm:
            # virtually increase max_epochs to perform batch norm update on latest epoch.
            trainer.max_epochs += 1

    def on_train_epoch_start(self, trainer, pl_module):
        if trainer.current_epoch == self.swa_start:
            # move average model to request device.
            self._average_model = self._average_model.to(self._device or pl_module.device)

            optimizers = trainer.optimizers
            lr_scheduler = trainer.lr_schedulers[0]["scheduler"]

            self._swa_scheduler = SWALR(
                optimizers[0],
                swa_lr=self._swa_lrs,
                anneal_epochs=self._annealing_epochs,
                anneal_strategy=self._annealing_strategy,
                last_epoch=trainer.max_epochs if self._annealing_strategy == "cos" else -1
            )

            rank_zero_warn(f"Swapping lr_scheduler {lr_scheduler} for {self._swa_scheduler}")
            trainer.lr_schedulers[0]["scheduler"] = self._swa_scheduler

            self.n_averaged = torch.tensor(0, dtype=torch.long, device=pl_module.device)

        if self.swa_start <= trainer.current_epoch <= self.swa_end:
            self.update_parameters(self._average_model, pl_module, self.n_averaged, self.avg_fn)

        if trainer.current_epoch > self.swa_end:
            # Transfer weights from average model to pl_module
            self.transfer_weights(self._average_model, pl_module)

            # Reset BatchNorm for update
            self.reset_batch_norm_and_save_state(pl_module)

            # There is no need to perform either backward or optimizeras we are
            # performing only one pass over the train dataloader to compute activation statistics
            # Therefore, we will virtually increase `num_training_batches` by 1 and skip backward.
            trainer.train_loop._skip_backward = True
            trainer.num_training_batches += 1
            self._accumulate_grad_batches = trainer.accumulate_grad_batches
            trainer.accumulate_grad_batches = len(trainer.train_dataloader)

    def on_train_epoch_end(self, trainer, pl_module, *args):
        # TODO: this should be done on_epoch_end but it is currently broken
        # and the PR to fix it hasn't been merged to this branch yet
        trainer.train_loop._skip_backward = False
        if trainer.current_epoch == self.swa_end and not self._model_contains_batch_norm:
            # Last SWA epoch. Transfer weights from average model to pl_module
            self.transfer_weights(self._average_model, pl_module)
        elif trainer.current_epoch > self.swa_end:
            # BatchNorm epoch update over. reset state
            trainer.accumulate_grad_batches = self._accumulate_grad_batches
            trainer.num_training_batches -= 1
            self.reset_momenta()

    @staticmethod
    def update_parameters(average_model, model, n_averaged, avg_fn):
        """
        Credit to PyTorch Team.
        Taken from https://github.com/pytorch/pytorch/blob/v1.7.1/torch/optim/swa_utils.py#L103
        """
        for p_swa, p_model in zip(average_model.parameters(), model.parameters()):
            device = p_swa.device
            p_swa_ = p_swa.detach()
            p_model_ = p_model.detach().to(device)
            src = p_model_ if n_averaged == 0 else avg_fn(p_swa_, p_model_, n_averaged.to(device))
            p_swa_.copy_(src)
        n_averaged += 1

    @staticmethod
    def transfer_weights(src_pl_module: 'pl.LightningModule', dst_pl_module: 'pl.LightningModule'):
        for src_param, dst_param in zip(src_pl_module.parameters(), dst_pl_module.parameters()):
            dst_param.detach().copy_(src_param.to(dst_param.device))

    @staticmethod
    def avg_fn(averaged_model_parameter, model_parameter, num_averaged) -> torch.FloatTensor:
        """
        Credit to PyTorch Team.
        Taken from https://github.com/pytorch/pytorch/blob/v1.7.1/torch/optim/swa_utils.py#L95
        """
        return averaged_model_parameter + \
            (model_parameter - averaged_model_parameter) / (num_averaged + 1)