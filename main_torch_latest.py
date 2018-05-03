import torch
from torch import optim
from torch.autograd import Variable
import torch.nn.functional as F
#import loadData3 as loadData
#import loadData2_latest as loadData
#import loadData
import numpy as np
import time
import os
#from LogMetric import Logger
import argparse
#from models.encoder_plus import Encoder
#from models.encoder import Encoder
#from models.encoder_bn_relu import Encoder
from models.encoder_vgg import Encoder
from models.decoder import Decoder
from models.attention import locationAttention as Attention
#from models.attention import TroAttention as Attention
from models.seq2seq import Seq2Seq
from utils import visualizeAttn, writePredict, writeLoss, HEIGHT, WIDTH, output_max_len, tokens, vocab_size, FLIP, WORD_LEVEL, load_data_func

parser = argparse.ArgumentParser(description='seq2seq net', formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument('start_epoch', type=int, help='load saved weights from which epoch')
args = parser.parse_args()

#torch.cuda.set_device(1)

Bi_GRU = True
VISUALIZE_TRAIN = True

BATCH_SIZE = 32
learning_rate = 2 * 1e-4
#lr_milestone = [30, 50, 70, 90, 120]
#lr_milestone = [20, 40, 60, 80, 100]
lr_milestone = [20, 40, 47, 60, 80, 100]
lr_gamma = 0.5

START_TEST = 1e4 # 1e4: never run test 0: run test from beginning
FREEZE = False
freeze_milestone = [65, 90]
EARLY_STOP_EPOCH = 100 # None: no early stopping
HIDDEN_SIZE_ENC = 256
HIDDEN_SIZE_DEC = 256 # model/encoder.py SUM_UP=False: enc:dec = 1:2  SUM_UP=True: enc:dec = 1:1
CON_STEP = None # CON_STEP = 4 # encoder output squeeze step
CurriculumModelID = args.start_epoch
#CurriculumModelID = -1 # < 0: do not use curriculumLearning, train from scratch
#CurriculumModelID = 170 # 'save_weights/seq2seq-170.model.backup'
EMBEDDING_SIZE = 60 # IAM
TRADEOFF_CONTEXT_EMBED = None # = 5 tradeoff between embedding:context vector = 1:5
TEACHER_FORCING = False
MODEL_SAVE_EPOCH = 1


def teacher_force_func(epoch):
    if epoch < 50:
        teacher_rate = 0.5
    elif epoch < 150:
        teacher_rate = (50 - (epoch-50)//2) / 100.
    else:
        teacher_rate = 0.
    return teacher_rate

def teacher_force_func_2(epoch):
    if epoch < 200:
        teacher_rate = (100 - epoch//2) / 100.
    else:
        teacher_rate = 0.
    return teacher_rate


def all_data_loader():
    data_train, data_valid, data_test = load_data_func()
    train_loader = torch.utils.data.DataLoader(data_train, collate_fn=sort_batch, batch_size=BATCH_SIZE, shuffle=True, num_workers=2, pin_memory=True)
    valid_loader = torch.utils.data.DataLoader(data_valid, collate_fn=sort_batch, batch_size=BATCH_SIZE, shuffle=False, num_workers=2, pin_memory=True)
    test_loader = torch.utils.data.DataLoader(data_test, collate_fn=sort_batch, batch_size=BATCH_SIZE, shuffle=False, num_workers=2, pin_memory=True)
    return train_loader, valid_loader, test_loader

def test_data_loader_batch(batch_size_nuevo):
    _, _, data_test = load_data_func()
    test_loader = torch.utils.data.DataLoader(data_test, collate_fn=sort_batch, batch_size=batch_size_nuevo, shuffle=False, num_workers=2, pin_memory=True)
    return test_loader

def sort_batch(batch):
    n_batch = len(batch)
    train_index = []
    train_in = []
    train_in_len = []
    train_out = []
    for i in range(n_batch):
        idx, img, img_width, label = batch[i]
        train_index.append(idx)
        train_in.append(img)
        train_in_len.append(img_width)
        train_out.append(label)

    train_index = np.array(train_index)
    train_in = np.array(train_in, dtype='float32')
    train_out = np.array(train_out, dtype='int64')
    train_in_len = np.array(train_in_len, dtype='int64')

    train_in = torch.from_numpy(train_in)
    train_out = torch.from_numpy(train_out)
    train_in_len = torch.from_numpy(train_in_len)

    train_in_len, idx = train_in_len.sort(0, descending=True)
    train_in = train_in[idx]
    train_out = train_out[idx]
    train_index = train_index[idx]
    return train_index, train_in, train_in_len, train_out

def train(train_loader, seq2seq, opt, teacher_rate, epoch):
    seq2seq.train()
    total_loss = 0
    for num, (train_index, train_in, train_in_len, train_out) in enumerate(train_loader):
        train_in = train_in.unsqueeze(1)
        train_in, train_out = Variable(train_in).cuda(), Variable(train_out).cuda()
        output, attn_weights = seq2seq(train_in, train_out, train_in_len, teacher_rate=teacher_rate, train=True) # (100-1, 32, 62+1)
        batch_count_n = writePredict(epoch, train_index, output, 'train')
        train_label = train_out.permute(1, 0)[1:].contiguous().view(-1)#remove<GO>
        output_l = output.view(-1, vocab_size) # remove last <EOS>

        if VISUALIZE_TRAIN:
            if 'e02-074-03-00,191' in train_index:
                b = train_index.tolist().index('e02-074-03-00,191')
                visualizeAttn(train_in.data[b,0], train_in_len[0], [j[b] for j in attn_weights], epoch, batch_count_n[b], 'train_e02-074-03-00')

        loss = F.cross_entropy(output_l.view(-1, vocab_size),
                               train_label, ignore_index=tokens['PAD_TOKEN'])
        opt.zero_grad()
        loss.backward()
        opt.step()
        total_loss += loss.data[0]

    total_loss /= (num+1)
    return total_loss

def valid(valid_loader, seq2seq, epoch):
    seq2seq.eval()
    total_loss_t = 0
    for num, (test_index, test_in, test_in_len, test_out) in enumerate(valid_loader):
        test_in = test_in.unsqueeze(1)
        test_in, test_out = Variable(test_in, volatile=True).cuda(), Variable(test_out, volatile=True).cuda()
        output_t, attn_weights_t = seq2seq(test_in, test_out, test_in_len, teacher_rate=False, train=False)
        batch_count_n = writePredict(epoch, test_index, output_t, 'valid')
        test_label = test_out.permute(1, 0)[1:].contiguous().view(-1)
        loss_t = F.cross_entropy(output_t.view(-1, vocab_size),
                                 test_label, ignore_index=tokens['PAD_TOKEN'])
        total_loss_t += loss_t.data[0]

        if 'n04-015-00-01,171' in test_index:
            b = test_index.tolist().index('n04-015-00-01,171')
            visualizeAttn(test_in.data[b,0], test_in_len[0], [j[b] for j in attn_weights_t], epoch, batch_count_n[b], 'valid_n04-015-00-01')
    total_loss_t /= (num+1)
    return total_loss_t

def test(test_loader, modelID, showAttn=True):
    encoder = Encoder(HIDDEN_SIZE_ENC, HEIGHT, WIDTH, Bi_GRU, CON_STEP, FLIP).cuda()
    decoder = Decoder(HIDDEN_SIZE_DEC, EMBEDDING_SIZE, vocab_size, Attention, TRADEOFF_CONTEXT_EMBED).cuda()
    seq2seq = Seq2Seq(encoder, decoder, output_max_len, vocab_size).cuda()
    model_file = 'save_weights/seq2seq-' + str(modelID) +'.model'
    print('Loading ' + model_file)
    seq2seq.load_state_dict(torch.load(model_file)) #load

    seq2seq.eval()
    total_loss_t = 0
    start_t = time.time()
    for num, (test_index, test_in, test_in_len, test_out) in enumerate(test_loader):
        test_in = test_in.unsqueeze(1)
        test_in, test_out = Variable(test_in, volatile=True).cuda(), Variable(test_out, volatile=True).cuda()
        output_t, attn_weights_t = seq2seq(test_in, test_out, test_in_len, teacher_rate=False, train=False)
        batch_count_n = writePredict(modelID, test_index, output_t, 'test')
        test_label = test_out.permute(1, 0)[1:].contiguous().view(-1)
        loss_t = F.cross_entropy(output_t.view(-1, vocab_size),
                                 test_label, ignore_index=tokens['PAD_TOKEN'])
        total_loss_t += loss_t.data[0]

        if showAttn:
            global_index_t = 0
            for t_idx, t_in in zip(test_index, test_in):
                visualizeAttn(t_in.data[0], test_in_len[0], [j[global_index_t] for j in attn_weights_t], modelID, batch_count_n[global_index_t], 'test_'+t_idx.split(',')[0])
                global_index_t += 1

    total_loss_t /= (num+1)
    writeLoss(total_loss_t, 'test')
    print('    TEST loss=%.3f, time=%.3f' % (total_loss_t, time.time()-start_t))

def main(train_loader, valid_loader):
    encoder = Encoder(HIDDEN_SIZE_ENC, HEIGHT, WIDTH, Bi_GRU, CON_STEP, FLIP).cuda()
    decoder = Decoder(HIDDEN_SIZE_DEC, EMBEDDING_SIZE, vocab_size, Attention, TRADEOFF_CONTEXT_EMBED).cuda()
    seq2seq = Seq2Seq(encoder, decoder, output_max_len, vocab_size).cuda()
    if CurriculumModelID > 0:
        model_file = 'save_weights/seq2seq-' + str(CurriculumModelID) +'.model'
        #model_file = 'save_weights/words/seq2seq-' + str(CurriculumModelID) +'.model'
        print('Loading ' + model_file)
        seq2seq.load_state_dict(torch.load(model_file)) #load
    opt = optim.Adam(seq2seq.parameters(), lr=learning_rate)
    #opt = optim.SGD(seq2seq.parameters(), lr=learning_rate, momentum=0.9)
    #opt = optim.RMSprop(seq2seq.parameters(), lr=learning_rate, momentum=0.9)

    #scheduler = optim.lr_scheduler.StepLR(opt, step_size=20, gamma=1)
    scheduler = optim.lr_scheduler.MultiStepLR(opt, milestones=lr_milestone, gamma=lr_gamma)
    epochs = 5000000
    if EARLY_STOP_EPOCH is not None:
        min_loss = 1e3
        min_loss_index = 0
        min_loss_count = 0

    if CurriculumModelID > 0 and WORD_LEVEL:
        start_epoch = CurriculumModelID + 1
        for i in range(start_epoch):
            scheduler.step()
    else:
        start_epoch = 0

    for epoch in range(start_epoch, epochs):
        scheduler.step()
        lr = scheduler.get_lr()[0]
        teacher_rate = teacher_force_func(epoch) if TEACHER_FORCING else False
        start = time.time()
        loss = train(train_loader, seq2seq, opt, teacher_rate, epoch)
        writeLoss(loss, 'train')
        print('epoch %d/%d, loss=%.3f, lr=%.8f, teacher_rate=%.3f, time=%.3f' % (epoch, epochs, loss, lr, teacher_rate, time.time()-start))

        if epoch%MODEL_SAVE_EPOCH == 0:
            folder_weights = 'save_weights'
            if not os.path.exists(folder_weights):
                os.makedirs(folder_weights)
            torch.save(seq2seq.state_dict(), folder_weights+'/seq2seq-%d.model'%epoch)

        start_v = time.time()
        loss_v = valid(valid_loader, seq2seq, epoch)
        writeLoss(loss_v, 'valid')
        print('  Valid loss=%.3f, time=%.3f' % (loss_v, time.time()-start_v))

        if EARLY_STOP_EPOCH is not None:
            if loss_v < min_loss:
                min_loss = loss_v
                min_loss_index = epoch
                min_loss_count = 0
            else:
                min_loss_count += 1
            if min_loss_count >= EARLY_STOP_EPOCH:
                print('Early Stopping at: %d. Best epoch is: %d' % (epoch, min_loss_index))
                return min_loss_index

if __name__ == '__main__':
    print(time.ctime())
    train_loader, valid_loader, test_loader = all_data_loader()
    mejorModelID = main(train_loader, valid_loader)
    test(test_loader, mejorModelID, True)
    os.system('./test.sh '+str(mejorModelID))
    print(time.ctime())
