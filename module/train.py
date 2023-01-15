import time, math, json, torch
import torch.nn as nn
import torch.amp as amp
import torch.optim as optim



class TrainerBase:
    def __init__(self, config):
        super(TrainerBase, self).__init__()
        
        self.src = config.src
        self.trg = config.trg
        self.task = config.task
        self.clip = config.clip
        self.device = config.device
        self.n_epochs = config.n_epochs
        self.vocab_size = config.vocab_size
        self.device_type = config.device_type
        self.scaler = torch.cuda.amp.GradScaler()
        self.iters_to_accumulate = config.iters_to_accumulate        
        
        self.ckpt = config.ckpt
        self.record_path = f"ckpt/{config.task}_{config.model_name}.json"


    def print_epoch(self, record_dict):
        print(f"""Epoch {record_dict['epoch']}/{self.n_epochs} | \
              Time: {record_dict['train_time']}""".replace(' ' * 14, ''))
        
        print(f"""  >> Train Loss: {record_dict['train_loss']:.3f} | \
              Train PPL: {record_dict['train_ppl']:.2f}""".replace(' ' * 14, ''))

        print(f"""  >> Valid Loss: {record_dict['valid_loss']:.3f} | \
              Valid PPL: {record_dict['valid_ppl']:.2f}\n""".replace(' ' * 14, ''))


    @staticmethod
    def measure_time(start_time, end_time):
        elapsed_time = end_time - start_time
        elapsed_min = int(elapsed_time / 60)
        elapsed_sec = int(elapsed_time - (elapsed_min * 60))
        return f"{elapsed_min}m {elapsed_sec}s"        


    def split_batch(self, batch):
        input_ids = batch[f'{self.src}_ids'].to(self.device)
        attention_mask =  batch[f'{self.src}_mask'].to(self.device)
        labels = batch[f'{self.trg}_ids'].to(self.device)        
        return input_ids, attention_mask, labels        


    def save_ckpt(self, epoch, ckpt, model, optimizer):
        torch.save({'epoch': epoch,
                    'model_state_dict': model.state_dict(),
                    'optimizer_state_dict': optimizer.state_dict()},
                    ckpt)


class Trainer(TrainerBase):
    def __init__(self, config, generator, discriminator, train_dataloader, valid_dataloader):
        super(Trainer, self).__init__(config)

        self.generator = generator
        self.discriminator = discriminator

        self.train_dataloader = train_dataloader
        self.valid_dataloader = valid_dataloader
        
        self.record_keys = ['epoch', 'gen_train_loss', 'gen_valid_loss',
                            'dis_train_loss', 'dis_valid_loss',  
                            'gen_lr', 'dis_lr', 'train_time']
        
        #criterion for discriminator
        self.criterion = nn.BCELoss(ignore_index=config.pad_id, 
                                    label_smoothing=0.1).to(self.device)

        self.gen_optimizer = optim.AdamW(params=self.generator.parameters(), lr=lr)
        self.dis_optimizer = optim.AdamW(params=self.discriminator.parameters(), lr=lr)

        #set lr scheduler for generator
        self.scheduler = optim.lr_scheduler.ReduceLROnPlateau(self.gen_optimizer, 'min')



    def train(self):
        gen_best_loss, dis_best_loss, records = float('inf'), float('inf'), []
        for epoch in range(1, self.n_epochs + 1):
            start_time = time.time()

            record_vals = [epoch, *self.train_epoch(), *self.valid_epoch(), 
                           self.gen_optimizer.param_groups[0]['lr'],
                           self.dis_optimizer.param_groups[0]['lr'],
                           self.measure_time(start_time, time.time())]
            record_dict = {k: v for k, v in zip(self.record_keys, record_vals)}
            
            records.append(record_dict)
            self.print_epoch(record_dict)
            
            gen_curr_loss = record_dict['gen_valid_loss']
            dis_curr_loss = record_dict['dis_valid_loss']
            self.scheduler.step(gen_loss) #scheduling generator only!


            #save best generator states
            if gen_best_loss >= gen_curr_loss:
                gen_best_loss = gen_curr_loss
                self.save_ckpt(epoch, self.ckpt, self.generator, self.gen_optimizer)

            #save best discriminator states
            if dis_best_loss >= dis_curr_loss:
                dis_best_loss = dis_curr_loss
                self.save_ckpt(epoch, self.ckpt, self.discriminator, self.dis_optimizer)

        #save train_records
        with open(self.record_path, 'w') as fp:
            json.dump(records, fp)


    def get_losses(self, input_ids, attention_mask, labels):
        batch_size = input_ids.size(0)

        samples = self.generator.generate(input_ids=input_ids, 
                                          max_new_tokens=labels.size(-1), 
                                          use_cache=True)
        
        dis_inputs = torch.cat((samples, labels), dim=-1)
        indices = torch.randperm(batch_size * 2)        
        
        dis_inputs = dis_inputs[indices]
        dis_labels = indices[indices > batch_size]

        with torch.autocast(device_type=self.device_type, dtype=torch.float16):
       
            gen_loss = self.generator(input_ids=input_ids, 
                                      attention_mask=attention_mask,
                                      labels=labels).loss

            dis_logit = self.discriminator(inputs=dis_inputs)
            dis_loss = self.criterion(dis_logit, dis_labels)

        return gen_loss + dis_logit, dis_loss


    def train_epoch(self):
        self.generator.train()
        self.discriminator.train()

        gen_epoch_loss, dis_epoch_loss = 0, 0
        tot_len = len(self.train_dataloader)

        for idx, batch in enumerate(self.train_dataloader):
            input_ids, attention_mask, labels = self.split_batch(batch)

            gen_loss, dis_loss = self.get_losses(self, input_ids, attention_mask, labels)

            gen_loss = gen_loss / self.iters_to_accumulate
            dis_loss = dis_loss / self.iters_to_accumulate

            self.scaler.scale(gen_loss).backward()
            self.scaler.scale(dis_loss).backward()
            
            if (idx + 1) % self.iters_to_accumulate == 0:
                #Gradient Clipping
                self.scaler.unscale_(self.optimizer)
                nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=self.clip)
                
                #Gradient Update & Scaler Update
                self.scaler.step(self.optimizer)
                self.scaler.update()
                self.optimizer.zero_grad()

            gen_epoch_loss += gen_loss.item()
            dis_epoch_loss += dis_loss.item()
        
        gen_epoch_loss = round(gen_epoch_loss / tot_len, 3)
        dis_epoch_loss = round(dis_epoch_loss / tot_len, 3)
        return gen_epoch_loss, gen_epoch_loss
    


    def valid_epoch(self):
        self.generator.eval()
        self.discriminator.eval()

        gen_epoch_loss, dis_epoch_loss = 0, 0
        tot_len = len(self.valid_dataloader)
        
        with torch.no_grad():
            for _, batch in enumerate(self.valid_dataloader):   
                input_ids, attention_mask, labels = self.split_batch(batch)           
                
                gen_loss, dis_loss = self.get_losses(self, input_ids, attention_mask, labels)

                gen_epoch_loss += gen_loss.item()
                dis_epoch_loss += dis_loss.item()
    
        gen_epoch_loss = round(gen_epoch_loss / tot_len, 3)
        dis_epoch_loss = round(dis_epoch_loss / tot_len, 3)
        return gen_epoch_loss, gen_epoch_loss