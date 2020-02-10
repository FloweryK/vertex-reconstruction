from utils import mkdir, load, save, path_list
from utils import DEAD_PMTS
from nets import Cnn1c, Cnn2c, Net

import time
import json
import pprint
import argparse
import datetime
import numpy as np
import pandas as pd
from itertools import repeat
from multiprocessing import Pool
import warnings

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader

# for RuntimeError: see https://discuss.pytorch.org/t/runtimeerror-received-0-items-of-ancdata/4999
torch.multiprocessing.set_sharing_strategy('file_system')


class JsonDataset(Dataset):
    def __init__(self, paths, mode='hit', input_type='prompt', output_type='prompt', dead=False):
        self.paths = paths
        self.len = len(self.paths)
        self.mode = mode
        self.input_type = input_type
        self.output_type = output_type
        self.is_dead_PMT = dead

    def __getitem__(self, index):
        return self.get_x(self.paths[index]), self.get_y(self.paths[index])

    def __len__(self):
        return self.len

    def get_x(self, path):
        with open(path, encoding='UTF8') as j:
            f = json.load(j)

            hits = int(f['photon_hits'])        # scalar value
            capture_time = f['capture_time']    # scalar value

            hit_pmts = f['hit_pmt']             # vector value
            hit_time = f['hit_time']            # vector value
            hit_counts = f['hit_count']         # vector value

            if self.mode == 'hit':
                # fill a single data
                x = np.zeros(354)
                for i in range(hits):
                    pmt = hit_pmts[i]
                    t = hit_time[i]
                    count = hit_counts[i]

                    # breakpoint
                    if self.is_dead_PMT and (pmt in DEAD_PMTS):
                        continue

                    # get prompted signal (before capture) or delayed signal (after capture)
                    if self.input_type == 'prompt':
                        if t < capture_time:
                            x[pmt] += count
                    elif self.input_type == 'delayed':
                        if t > capture_time:
                            x[pmt] += count
                    else:
                        x[pmt] += count

                # normalizing
                if np.max(x) > 0:
                    x *= 1 / np.max(x)

                # converting input into [1, 354] format (1 channel)
                x = np.array([x.tolist()], dtype=float)
                return x

            elif self.mode == 'time':
                # Fill a single data
                x = np.empty([354, hits])
                x[:] = np.nan

                for i in range(hits):
                    pmt = hit_pmts[i]
                    t = hit_time[i]

                    # Breakpoint
                    if self.is_dead_PMT and (pmt in DEAD_PMTS):
                        continue

                    # Get prompted signal (before capture) or delayed signal (after capture)
                    if self.input_type == 'prompt':
                        if t < capture_time:
                            x[pmt][i] = t
                    elif self.input_type == 'delayed':
                        if t > capture_time:
                            x[pmt][i] = t
                    else:
                        x[pmt][i] = t

                # Extract the first hit
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', category=RuntimeWarning)
                    x = np.nanmean(x, axis=1)
                x = np.nan_to_num(x, nan=.0)

                # Normalizing
                if np.max(x) > 0:
                    x *= 1 / np.max(x)

                # converting input into [1, 354] format (1 channel)
                x = np.array([x.tolist()], dtype=float)
                return x

            elif self.mode == 'hit-time':
                # fill a single data
                x_hit = np.zeros(354)
                if hits:
                    x_time = np.empty([354, hits])
                    x_time[:] = np.nan
                else:
                    x_time = np.zeros([354, 1])

                for i in range(hits):
                    pmt = hit_pmts[i]
                    t = hit_time[i]
                    count = hit_counts[i]

                    # breakpoint
                    if self.is_dead_PMT and (pmt in DEAD_PMTS):
                        continue

                    # get prompted signal (before capture) or delayed signal (after capture)
                    if self.input_type == 'prompt':
                        if t < capture_time:
                            x_hit[pmt] += count
                            x_time[pmt][i] = t
                    elif self.input_type == 'delayed':
                        if t > capture_time:
                            x_hit[pmt] += count
                            x_time[pmt][i] = t
                    else:
                        x_time[pmt][i] = t

                # Extract the first hit
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', category=RuntimeWarning)
                    x_time = np.nanmin(x_time, axis=1)
                x_time = np.nan_to_num(x_time, nan=.0)

                # normalizing
                if np.max(x_hit) > 0:
                    x_hit *= 1 / np.max(x_hit)
                if np.max(x_time) > 0:
                    x_time *= 1 / np.max(x_time)

                # converting input into [1, 354] format (1 channel)
                x = np.array([x_hit.tolist(), x_time.tolist()], dtype=float)
                return x

            else:
                print('invalid argument for mode:', self.mode)
                print('please select among "hit" or "time"')
                raise ValueError

    def get_y(self, path):
        with open(path, encoding='UTF8') as j:
            f = json.load(j)

            # vertices for label
            if self.output_type == 'prompt':
                vertex_x0 = f['positron_x']
                vertex_y0 = f['positron_y']
                vertex_z0 = f['positron_z']
            elif self.output_type == 'delayed':
                vertex_x0 = f['cap_neu_x']
                vertex_y0 = f['cap_neu_y']
                vertex_z0 = f['cap_neu_z']
            else:
                vertex_x0 = f['vertex_x0']
                vertex_y0 = f['vertex_y0']
                vertex_z0 = f['vertex_z0']

            vertex = [vertex_x0, vertex_y0, vertex_z0]
            y = np.array(vertex) / 1000  # [-1, 1] transform

        return y


def load_all(dataloader):
    total = len(dataloader)
    start = time.time()

    inputs = []
    labels = []
    for i, data in enumerate(dataloader):
        inputs.append(data[0])
        labels.append(data[1])
        print('data loading %i%% (%i/%i, %.1fs)' % (int(100 * i / total), i, total, time.time()-start),
              end='\n' if i == (total - 1) else '\r')

    inputs = torch.cat(inputs, 0)
    labels = torch.cat(labels, 0)

    print('inputs:', inputs.size())
    print('labels:', labels.size())
    print(f'data loading took {time.time() - start:.1f} secs')

    return inputs, labels


def __job_filter_zero_counts(path, input_type):
    f = load(path)

    hits = int(f['photon_hits'])  # scalar value
    hit_time = f['hit_time']  # vector value
    hit_counts = f['hit_count']  # vector value
    capture_time = f['capture_time']  # scalar value

    valid_counts = 0
    for i in range(hits):
        count = hit_counts[i]
        t = hit_time[i]

        # get prompted signal (before capture) or delayed signal (after capture)
        if input_type == 'prompt':
            if t < capture_time:
                valid_counts += count
        elif input_type == 'delayed':
            if t >= capture_time:
                valid_counts += count
        else:
            valid_counts += count

    if (valid_counts > 0) and (valid_counts < sum(hit_counts)):
        return True
    else:
        return False


def filter_zero_counts(paths, input_type):
    p = Pool(processes=40)

    start = time.time()
    is_not_empty = []
    for i in range(100):
        print('data filtering %2i%%, %.2fs' % (i, time.time()-start), end='\n' if i == 99 else '\r')
        paths_batch = paths[int(0.01*i*len(paths)):int(0.01*(i+1)*len(paths))]
        is_not_empty += p.starmap(__job_filter_zero_counts, zip(paths_batch, repeat(input_type)))

    filtered_paths = [paths[i] for i in range(len(is_not_empty)) if is_not_empty[i]]
    print('after filtering: %i -> %i (%.2f%%)' % (len(paths), len(filtered_paths), 100 * len(filtered_paths) / len(paths)))

    return filtered_paths


def main():
    # Argument configuration
    parser = argparse.ArgumentParser()
    # general
    parser.add_argument('--root', type=str, default='MC', help='MC root directory')
    parser.add_argument('--mode', type=str, default='hit', help='mode for input (hit, time)')
    parser.add_argument('--input', type=str, default='prompt', help='input type (prompt, delated, all)')
    parser.add_argument('--output', type=str, default='prompt', help='output type (prompt, delated, all)')

    # control
    parser.add_argument('--data', type=int, default=0, help='number of dataset, if 0, use all')
    parser.add_argument('--dead', type=int, default=0, help='is dead PMT on or not.')
    parser.add_argument('--fast', type=int, default=0, help='for testing, skip filtering.')

    # learning
    parser.add_argument('--lr', type=float, default=1e-4, help='learning rate')
    parser.add_argument('--batch', type=int, default=128, help='batch size, multiplied by cuda device number')
    parser.add_argument('--worker', type=int, default=40, help='num_worker of dataloader')
    parser.add_argument('--epoch', type=int, default=40, help='number of epochs')

    # optional
    parser.add_argument('--text', type=str, default='', help='additional text to test save directory')

    args = parser.parse_args()
    root_directory = args.root
    mode = args.mode
    input_type = args.input
    output_type = args.output

    num_dataset = args.data
    dead_pmt = args.dead
    fast = args.fast

    lr = args.lr
    batch_size = args.batch * (torch.cuda.device_count() if torch.cuda.is_available() else 1)
    num_worker = args.worker
    num_epochs = args.epoch

    text = args.text

    # Make save directory
    save_directory = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    save_directory += '-' + root_directory
    save_directory += '-' + mode
    save_directory += '-' + input_type + '_' + output_type
    save_directory += '-e' + str(num_epochs)
    if num_dataset:
        save_directory += '-d' + str(int(num_dataset / 1000)) + 'k'
    if text:
        save_directory += '-' + text

    # Load dataset paths
    paths = path_list(root_directory, filter='.json', shuffle=True)
    if not fast:
        print('not fast: data filtering')
        paths = filter_zero_counts(paths, input_type)
    if num_dataset:
        paths = paths[:num_dataset]

    # Prepare trainset
    trainpaths = paths[:int(len(paths) * 0.8)]
    trainset = JsonDataset(paths=trainpaths, mode=mode, input_type=input_type, output_type=output_type, dead=dead_pmt)
    trainloader = DataLoader(trainset, batch_size=batch_size, shuffle=True, num_workers=num_worker)
    save(trainpaths, save_directory + '/trainpaths.list')

    # prepare valiset
    valipaths = paths[int(len(paths) * 0.8):int(len(paths) * 0.9)]
    valiset = JsonDataset(paths=valipaths, mode=mode, input_type=input_type, output_type=output_type, dead=dead_pmt)
    valiloader = DataLoader(valiset, batch_size=batch_size, shuffle=True, num_workers=num_worker)
    vali_inputs, vali_labels = load_all(valiloader)
    save(valipaths, save_directory + '/valipaths.list')
    save(vali_inputs, save_directory + '/vali_inputs.tensor')
    save(vali_labels, save_directory + '/vali_labels.tensor')

    # prepare testset
    testpaths = paths[int(len(paths) * 0.9):]
    testset = JsonDataset(paths=testpaths, mode=mode, input_type=input_type, output_type=output_type, dead=dead_pmt)
    testloader = DataLoader(testset, batch_size=batch_size, shuffle=True, num_workers=num_worker)
    test_inputs, test_labels = load_all(testloader)
    save(testpaths, save_directory + '/testpaths.list')
    save(test_inputs, save_directory + '/test_inputs.tensor')
    save(test_labels, save_directory + '/test_labels.tensor')

    # Network, criterion, optimizer
    if mode == 'hit':
        net = Net()
    elif mode == 'time':
        net = Cnn1c()
    elif mode == 'hit-time':
        net = Cnn2c()
    else:
        print('invalide mode:', mode, ', please select from (hit, time, hit-time')
        raise ValueError
    criterion = nn.MSELoss()
    optimizer = optim.Adam(net.parameters(), lr=lr)

    # Data parallelism
    if torch.cuda.device_count() > 1:
        print('currently using', str(torch.cuda.device_count()), 'cuda devices.')
        net = nn.DataParallel(net)
    net = net.float()                   # Runtime error handling for float type to use Data parallelism.
    vali_inputs = vali_inputs.float()
    vali_labels = vali_labels.float()

    # GPU usage: move data to device
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    net.to(device)
    vali_inputs = vali_inputs.to(device)
    vali_labels = vali_labels.to(device)

    # Configuration summary
    config = {
        'root_directory': root_directory,
        'save directory': save_directory,
        'mode': mode,
        'input_type': input_type,
        'output_type': output_type,
        'lr': lr,
        'batch_size': batch_size,
        'num_worker': num_worker,
        'num_epochs': num_epochs,
        'number of data using:': num_dataset if num_dataset else 'full load',
        'model': {l[0]: str(l[1]) for l in net.named_children()},
        'using_dead_pmt': dead_pmt,
    }
    save(config, save_directory + '/configuration.json')

    # Check start time
    start_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    loss_history = {}
    for epoch in range(num_epochs):
        for i, data in enumerate(trainloader):
            # Get inputs and labels
            train_inputs, train_labels = data

            # Data parallelism: Runtime error handling
            train_inputs = train_inputs.float()
            train_labels = train_labels.float()

            # GPU usage: Move data to device
            train_inputs = train_inputs.to(device)
            train_labels = train_labels.to(device)

            # Get outputs
            optimizer.zero_grad()
            train_outputs = net(train_inputs)

            # Evaluate loss and optimize (update network)
            loss = criterion(train_outputs, train_labels)
            loss.backward()
            optimizer.step()

            # Get validation results
            if i % 100 == 0:
                vali_outputs = net(vali_inputs)
                vali_outputs = vali_outputs.detach().cpu().clone().numpy()
                try:
                    vali_labels = vali_labels.detach().cpu().clone().numpy()
                except AttributeError:
                    pass

                vali_dis = (vali_outputs - vali_labels) * 1000
                vali_sigma = np.std(vali_dis, axis=0)
                vali_mu = np.mean(vali_dis, axis=0)
                vali_loss = np.mean(vali_dis**2)

                dframe = {
                    'axis': ['x', 'y', 'z'],
                    'vali_sigma': vali_sigma,
                    'vali_mu': vali_mu,
                    'vali[0]': vali_outputs[0],
                    'labels[0]': vali_labels[0]
                }
                dframe = pd.DataFrame(dframe).T

                print('===========================================')
                print(datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'started at:', start_time)
                print('epoch: %02i (%04i/%i)' % (epoch, i, len(trainloader)))
                print('train loss(mm2)=%.1f, vali loss(mm2)=%.1f' % (loss.item() * 1600*1600, vali_loss))
                print(dframe)

                if epoch not in loss_history:
                    loss_history[epoch] = {}
                loss_history[epoch][i] = loss.item()

                save(loss_history, save_directory + '/loss_history.json')
                mkdir(f'{save_directory}/models/epoch_{epoch:05}/')

                if torch.cuda.device_count() > 1:
                    # if using data parallelism, you should save model in module.state_dict()
                    torch.save(net.module.state_dict(), f'{save_directory}/models/epoch_{epoch:05}/{i:05}.pt')
                else:
                    torch.save(net.state_dict(), f'{save_directory}/models/epoch_{epoch:05}/{i:05}.pt')


if __name__ == '__main__':
    main()

