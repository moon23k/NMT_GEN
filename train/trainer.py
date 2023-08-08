import time, math, json, torch
import torch.nn as nn
import torch.amp as amp
import torch.optim as optim

from optim import AdamW
from optim.lr_scheduler import ReduceLROnPlateau

from .standard import StandardTrainer
from .alternate import AlternateTrainer
from .generative import GenerativeTrainer
from .complementary import ComplementaryTrainer



class TrainerBase(object):
    def __init__(
        self, config, model, 
        tokenizer=None,
        train_dataloader=None, 
        valid_dataloader=None
        ):

        self.model = model
        self.clip = config.clip
        self.device = config.device
        self.n_epochs = config.n_epochs
        self.vocab_size = config.vocab_size

        self.device_type = config.device_type
        self.scaler = torch.cuda.amp.GradScaler()
        self.iters_to_accumulate = config.iters_to_accumulate        

        self.train_dataloader = train_dataloader
        self.valid_dataloader = valid_dataloader

        self.optimizer = AdamW(self.model.parameters(), lr=config.lr)
        self.scheduler = ReduceLROnPlateau(self.optimizer, 'min')

        self.early_stop = config.early_stop
        self.patience = config.patience
        
        self.ckpt = config.ckpt
        self.record_path = self.ckpt.replace(".pt", ".json")
        self.record_keys = ['epoch', 'train_loss', 'train_ppl',
                            'valid_loss', 'valid_ppl', 
                            'learning_rate', 'train_time']


    @staticmethod
    def measure_time(start_time, end_time):
        elapsed_time = end_time - start_time
        elapsed_min = int(elapsed_time / 60)
        elapsed_sec = int(elapsed_time - (elapsed_min * 60))
        return f"{elapsed_min}m {elapsed_sec}s"


    def print_epoch(self, record_dict):
        print(f"""Epoch {record_dict['epoch']}/{self.n_epochs} | \
              Time: {record_dict['train_time']}""".replace(' ' * 14, ''))
        
        print(f"""  >> Train Loss: {record_dict['train_loss']:.3f} | \
              Train PPL: {record_dict['train_ppl']:.2f}""".replace(' ' * 14, ''))

        print(f"""  >> Valid Loss: {record_dict['valid_loss']:.3f} | \
              Valid PPL: {record_dict['valid_ppl']:.2f}\n""".replace(' ' * 14, ''))



    def train(self):
        records = []
        prev_loss, best_loss = float('inf'), float('inf')
        patience = self.patience

        for epoch in range(1, self.n_epochs + 1):
            start_time = time.time()

            record_vals = [
                epoch, *self.train_epoch(), *self.valid_epoch(), 
                self.optimizer.param_groups[0]['lr'],
                self.measure_time(start_time, time.time())
            ]
            record_dict = {k: v for k, v in zip(self.record_keys, record_vals)}
            
            records.append(record_dict)
            self.print_epoch(record_dict)
            
            val_loss = record_dict['valid_loss']
            self.scheduler.step(val_loss)

            #save best model
            if best_loss > val_loss:
                best_loss = val_loss
                torch.save({'epoch': epoch,
                            'model_state_dict': self.model.state_dict(),
                            'optimizer_state_dict': self.optimizer.state_dict()},
                            self.ckpt)

            #Early Stopping Process
            if self.early_stop:
                if prev_loss > val_loss:
                    patience = self.patience
            
                else:
                    patience -= 1
                    if not patience:
                        print('--- Training Ealry Stopped ---\n')
                        break

                prev_loss = val_loss

            
        #save train_records
        with open(self.record_path, 'w') as fp:
            json.dump(records, fp)




class Trainer(object):
    def __init__(
        self, config, model, train_dataloader, valid_dataloader, tokenizer=None
        ):

        if config.train_type in ['alternate', 'generative']:
            assert tokenizer is not None
            
        self.tokenizer = tokenizer
        self.instance = self._get_trainer_instance(
            config, model, train_dataloader, valid_dataloader, tokenizer
        )


    def _get_trainer_instance(
        self, config, model, train_dataloader, valid_dataloader
        ):
        
        if config.train_type == "standard":
            return StandardTrainer(
                config, model, train_dataloader, valid_dataloader
            )
        elif config.train_type == "alternate":
            return AlternateTrainer(
                config, model, self.tokenizer, train_dataloader, valid_dataloader
            )
        elif config.train_type == "generative":
            return GenerativeTrainer(
                config, model, self.tokenizer, train_dataloader, valid_dataloader
            )
        elif config.train_type == "complementary":
            return ComplementaryTrainer(
                config, model, train_dataloader, valid_dataloader
            )


    def train(self):
        self.instance.train()