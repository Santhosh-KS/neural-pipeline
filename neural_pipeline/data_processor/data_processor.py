import json
import numpy as np

import torch
from tqdm import tqdm

from .model import Model
from ..train_config.train_config import TrainConfig
from ..utils.file_structure_manager import FileStructManager
from ..utils.utils import dict_recursive_bypass


class DataProcessor:
    """
    Class, that get data from data_conveyor and process it:
    1) Train or predict data
    2) Provide monitoring (showing metrics to console and tesorboard)
    """

    def __init__(self, model, file_struct_manager: FileStructManager, is_cuda=True):
        """
        :param model: model object
        :param file_struct_manager: file structure manager
        """
        self._is_cuda = is_cuda
        self._file_struct_manager = file_struct_manager

        self._model = Model(model, file_struct_manager)

        if self._is_cuda:
            self._model.model().cuda()

    def model(self):
        """
        Get model
        """
        return self._model.model()

    def predict(self, data) -> object:
        """
        Make predict by data
        :param data: data in dict
        :return: processed output
        """

        def make_predict():
            if self._is_cuda:
                data['data'] = self._process_data(data['data'])
            return self._model(data['data'])

        self._model.model().eval()
        with torch.no_grad():
            output = make_predict()
        return output

    def load(self) -> None:
        """
        Load from checkpoint
        """
        self._model.load_weights()

    @staticmethod
    def _process_data(data) -> torch.Tensor or dict:
        if isinstance(data, dict):
            return dict_recursive_bypass(data, lambda v: v.to('cuda:0'))
        else:
            return data.to('cuda:0')


class TrainDataProcessor(DataProcessor):
    def __init__(self, model, train_pipeline: TrainConfig, file_struct_manager: FileStructManager, is_cuda=True):
        super().__init__(model, file_struct_manager, is_cuda)

        self.metrics_processor = train_pipeline.metrics_processor()
        self.__criterion = train_pipeline.loss()

        if self._is_cuda:
            self.__criterion.to('cuda:0')

        self.__learning_rate = train_pipeline.learning_rate()
        self.__optimizer = train_pipeline.optimizer()

        self.__epoch_num = 0
        self.__val_loss_values, self.__train_loss_values = np.array([]), np.array([])

    def predict(self, data, is_train=False) -> np.array:
        """
        Make predict by data
        :param data: data in dict
        :param is_train: is data processor need train on data or just predict
        :return: processed output
        """

        def make_predict():
            if self._is_cuda:
                data['data'] = self._process_data(data['data'])
            return self._model(data['data'])

        if is_train:
            self._model.model().train()
            output = make_predict()
        else:
            self._model.model().eval()
            output = super().predict(data)

        return output

    def process_batch(self, batch: {}, is_train: bool) -> None:
        """
        Process one batch of data
        :param batch: dict, contains 'data' and 'target' keys. The values for key must be instance of torch.Tensor or dict
        :param is_train: is it is a train stage
        """
        if self._is_cuda:
            batch['target'] = self._process_data(batch['target'])

        if is_train:
            self.__optimizer.zero_grad()
        res = self.predict(batch, is_train)

        self.metrics_processor.calc_metrics(res, batch['target'], is_train)

        loss = self.__criterion(res, batch['target'])
        if is_train:
            loss.backward()
            self.__optimizer.step()

        loss_arr = loss.data.cpu().numpy()
        if is_train:
            self.__train_loss_values = np.append(self.__train_loss_values, loss_arr)
        else:
            self.__val_loss_values = np.append(self.__val_loss_values, loss_arr)

    def train_epoch(self, train_dataloader, validation_dataloader, epoch_idx: int, stage_name: str = None) -> {}:
        """
        Train one epoch
        :param train_dataloader: dataloader with train dataset
        :param validation_dataloader: dataloader with validation dataset
        :param epoch_idx: index of epoch
        :return: dict of events
        :param stage_name: name of stage for print in tqdm bar like 'train_<stage_name>'
        """
        with tqdm(train_dataloader, desc="train" if stage_name is None else "train_" + stage_name, leave=False) as t:
            for batch in t:
                self.process_batch(batch, is_train=True)
                t.set_postfix({'loss': '[{:4f}]'.format(self.__train_loss_values[-1])})
        with tqdm(validation_dataloader, desc="validation" if stage_name is None else "validation_" + stage_name, leave=False) as t:
            for batch in t:
                self.process_batch(batch, is_train=False)
                t.set_postfix({'loss': '[{:4f}]'.format(self.__val_loss_values[-1])})

        self.__epoch_num = epoch_idx

    def update_lr(self, lr: float) -> None:
        """
        Provide learning rate decay for optimizer
        :param lr: target learning rate
        """
        for param_group in self.__optimizer.param_groups:
            param_group['lr'] = lr

    def get_last_epoch_idx(self):
        return self.__epoch_num

    def get_state(self) -> {}:
        """
        Get model and optimizer state dicts
        """
        return {'weights': self._model.model().state_dict(), 'optimizer': self.__optimizer.state_dict()}

    def load(self):
        super().load()

        print("Data processor inited by file: ", self._file_struct_manager.optimizer_state_file(), end='; ')
        state = torch.load(self._file_struct_manager.optimizer_state_file())
        print('state dict len before:', len(state), end='; ')
        state = {k: v for k, v in state.items() if k in self.__optimizer.state_dict()}
        print('state dict len after:', len(state), end='; ')
        self.__optimizer.load_state_dict(state)
        print('done')

        with open(self._file_struct_manager.data_processor_state_file(), 'r') as in_file:
            dp_state = json.load(in_file)
            self.__epoch_num = dp_state['last_epoch_idx']
            self.update_lr(dp_state['lr'])
            self.__learning_rate.set_value(dp_state['lr'])

    def save_state(self) -> None:
        """
        Save state of optimizer and perform epochs number
        """
        torch.save(self.__optimizer.state_dict(), self._file_struct_manager.optimizer_state_file())

        with open(self._file_struct_manager.data_processor_state_file(), 'w') as out:
            json.dump({"last_epoch_idx": self.__epoch_num, 'lr': self.__learning_rate.value()}, out)

        self._model.save_weights()

    def get_losses(self) -> {}:
        return {'train': self.__val_loss_values, 'validation': self.__train_loss_values}

    def reset_losses(self) -> None:
        self.__val_loss_values, self.__train_loss_values = np.ndarray([]), np.ndarray([])