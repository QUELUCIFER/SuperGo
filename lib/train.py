import torch
import signal
import torch.nn as nn 
import numpy as np
import pickle
import time
import torch.nn.functional as F
import multiprocessing
import multiprocessing.pool
from lib.process import MyPool
from lib.dataset import SelfPlayDataset
from lib.evaluate import evaluate
from lib.utils import load_player
from copy import deepcopy
from pymongo import MongoClient
from torch.autograd import Variable
from torch.utils.data import DataLoader
from const import *
from models.agent import Player



class AlphaLoss(torch.nn.Module):
    """
    Custom loss as defined in the paper :
    (z - v) ** 2 --> MSE Loss
    (-pi * logp) --> Cross Entropy Loss
    z : self_play_winner
    v : winner
    pi : self_play_probas
    p : probas
    
    The loss is then averaged over the entire batch
    """

    def __init__(self):
        super(AlphaLoss, self).__init__()

    def forward(self, winner, self_play_winner, probas, self_play_probas):
        value_error = (self_play_winner - winner) ** 2
        policy_error = torch.sum((-self_play_probas * (1e-6 + probas).log()), 1)
        total_error = (value_error.view(-1) + policy_error).mean()
        return total_error


def fetch_new_games(collection, dataset, last_id, loaded_version=None):
    """ Update the dataset with new games from the databse """

    ## Fetch new games in reverse order so we add the newest games first
    new_games = collection.find({"id": {"$gt": last_id}}).sort('_id', -1)
    added_moves = 0
    added_games = 0
    print("[TRAIN] Fetching: %d new games from the db"% (new_games.count()))

    for game in new_games:
        number_moves = dataset.update(pickle.loads(game['game']))
        added_moves += number_moves
        added_games += 1

        ## You cant replace more than 40% of the dataset at a time
        if added_moves >= MOVES * MAX_REPLACEMENT and not loaded_version:
            break
    
    print("[TRAIN] Last id: %d, added games: %d, added moves: %d"\
                    % (last_id, added_games, added_moves))
    return last_id + added_games


def train_epoch(player, optimizer, example, criterion):
    """ Used to train the 3 models over a single batch """

    optimizer.zero_grad()
    winner, probas = player.predict(example['state'])

    loss = criterion(winner, example['winner'], \
                     probas, example['move'])
    loss.backward()
    optimizer.step()

    return float(loss)


def update_lr(lr, optimizer, total_ite, lr_decay=LR_DECAY, lr_decay_ite=LR_DECAY_ITE):
    """ Decay learning rate by a factor of lr_decay every lr_decay_ite iteration """

    if total_ite % lr_decay_ite != 0 or lr <= 0.0001:
        return lr, optimizer
    
    print("[TRAIN] Decaying the learning rate !")
    lr = lr * lr_decay
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr

    return lr, optimizer


def create_state(current_version, lr, total_ite, optimizer):
    """ Create a checkpoint to be saved """

    state = {
        'version': current_version,
        'lr': lr,
        'total_ite': total_ite,
        'optimizer': optimizer.state_dict()
    }
    return state


def collate_fn(example):
    """ Custom way of collating example in dataloader """

    state = []
    probas = []
    winner = []

    for ex in example:
        state.extend(ex[0])
        probas.extend(ex[1])
        winner.extend(ex[2])

    state = torch.tensor(state, dtype=torch.float, device=DEVICE)
    probas = torch.tensor(probas, dtype=torch.float, device=DEVICE)
    winner = torch.tensor(winner, dtype=torch.float, device=DEVICE)
    return state, probas, winner


def create_optimizer(player, lr, param=None):
    """ Create or load a saved optimizer """

    joint_params = list(player.extractor.parameters()) + \
                list(player.policy_net.parameters()) +\
                list(player.value_net.parameters())

    if ADAM:
        opt = torch.optim.Adam(joint_params, lr=lr, weight_decay=L2_REG)
    else:
        opt = torch.optim.SGD(joint_params, lr=lr, \
                        weight_decay=L2_REG, momentum=MOMENTUM)
    
    if param:
        opt.load_state_dict(param)
    
    return opt


def train(current_time, loaded_version):
    """ Train the models using the data generated by the self-play """

    last_id = 0
    total_ite = 1
    lr = LR
    version = 1
    pool = False 
    criterion = AlphaLoss()
    dataset = SelfPlayDataset()
    
    ## Database connection
    client = MongoClient()
    collection = client.superGo[current_time]

    ## First player either from disk or fresh
    if loaded_version:
        player, checkpoint = load_player(current_time, loaded_version) 
        optimizer = create_optimizer(player, lr, param=checkpoint['optimizer'])
        total_ite = checkpoint['total_ite']
        lr = checkpoint['lr']
        version = checkpoint['version']
        last_id = collection.find().count() - (MOVES // MOVE_LIMIT) * 2 
    else:
        player = Player()
        optimizer = create_optimizer(player, lr)
        state = create_state(version, lr, total_ite, optimizer)
        player.save_models(state, current_time)
    best_player = deepcopy(player)

    ## Callback after the evaluation is done, must be a closure
    def new_agent(result):
        if result:
            nonlocal version, pending_player, current_time, \
                    lr, total_ite, best_player 
            version += 1
            state = create_state(version, lr, total_ite, optimizer)
            best_player = pending_player
            pending_player.save_models(state, current_time)
            print("[EVALUATION] New best player saved !")
        else:
            nonlocal last_id
            ## Force a new fetch in case the player didnt improve
            last_id = fetch_new_games(collection, dataset, last_id)

    ## Wait before the circular before is full
    while len(dataset) < MOVES:
        last_id = fetch_new_games(collection, dataset, last_id, loaded_version=loaded_version)
        time.sleep(5)

    print("[TRAIN] Circular buffer full !")
    print("[TRAIN] Starting to train !")
    dataloader = DataLoader(dataset, collate_fn=collate_fn, \
                batch_size=BATCH_SIZE, shuffle=True)

    while True:
        batch_loss = []
        for batch_idx, (state, move, winner) in enumerate(dataloader):
            running_loss = []
            lr, optimizer = update_lr(lr, optimizer, total_ite)
    
            ## Evaluate a copy of the current network asynchronously
            if total_ite % TRAIN_STEPS == 0:
                pending_player = deepcopy(player)
                last_id = fetch_new_games(collection, dataset, last_id)

                ## Wait in case an evaluation is still going on
                if pool:
                    print("[EVALUATION] Waiting for eval to end before re-eval")
                    pool.close()
                    pool.join()
                pool = MyPool(1)
                try:
                    pool.apply_async(evaluate, args=(pending_player, best_player), \
                            callback=new_agent)
                except Exception as e:
                    client.close()
                    pool.terminate()
            
            example = {
                'state': state,
                'winner': winner,
                'move' : move
            }
            loss = train_epoch(player, optimizer, example, criterion)
            running_loss.append(loss)

            ## Print running loss
            if total_ite % LOSS_TICK == 0:
                batch_loss.append(np.mean(running_loss))
                print("[TRAIN] current iteration: %d, averaged loss: %.3f"\
                        % (total_ite, np.mean(running_loss)))
                running_loss = []
            
            ## Fetch new games
            if total_ite % REFRESH_TICK == 0:
                last_id = fetch_new_games(collection, dataset, last_id)
            
            total_ite += 1
    
        if len(batch_loss) > 0:
            print("[TRAIN] Batch loss : %.3f, current lr: %f" % (np.mean(batch_loss), lr))
    
