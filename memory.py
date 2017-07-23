import torch
import torch.autograd as ag
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import math
import functools

def random_uniform(shape, low, high):
    x = torch.rand(*shape)
    result = (high - low) * x + low
    return result

def multiply(x):
    return functools.reduce(lambda x,y: x*y, x, 1)

def flatten(x):
    """ Flatten matrix into a vector """
    count = multiply(x.size())
    return x.resize_(count)

def index(batch_size, x):
    idx = torch.arange(0, batch_size).long() 
    idx = torch.unsqueeze(idx, -1)
    return torch.cat((idx, x), dim=1)

def MemoryLoss(positive, negative, margin):
    """
        Calculate Average Memory Loss Function
        positive - positive cosine similarity
        negative - negative cosine similarity
        margin
    """
    assert(positive.size() == negative.size())
    dist_hinge = torch.clamp(negative - positive + margin, min=0.0)
    loss = torch.mean(dist_hinge)
    return loss

"""
Softmax Temperature -
    + Assume we have K elements at distance x. One element is at distance x+a
    + e^tm(x+a) / K*e^tm*x + e^tm(x+a) = e^tm*a / K + e^tm*a
    + For 20% probability, e^tm*a = 0.2K -> tm = ln(0.2 K)/a
"""

class Memory(nn.Module):
    def __init__(self, memory_size, key_dim, top_k = 256, inverse_temp = 40, age_noise=8.0, margin = 0.1):
        super(Memory, self).__init__()
        self.keys = F.normalize(torch.randn(memory_size, key_dim), dim=1)
        self.values = torch.zeros(memory_size, 1).long()
        self.age = torch.zeros(memory_size, 1)

        self.memory_size = memory_size
        self.key_dim = key_dim
        self.top_k = min(top_k, memory_size)
        self.softmax_temperature = max(1.0, math.log(0.2 * top_k) / inverse_temp)
        self.age_noise = age_noise
        self.margin = margin

    def predict(self, x):
        query = F.normalize(x, dim=1)
        keys_var = ag.Variable(self.keys, requires_grad=False)
        batch_size, dims = query.size()

        # Find the k-nearest neighbors of the query
        scores = torch.matmul(query, torch.t(keys_var))
        cosine_similarity, topk_indices_var = torch.topk(scores, self.top_k, dim=1)

        # retrive memory values - prediction
        y_hat_indices = topk_indices_var.data[:, 0]
        y_hat = self.values[y_hat_indices]

        # softmax of cosine similarities - embedding
        softmax_score = F.softmax(self.softmax_temperature * cosine_similarity)
        return y_hat, softmax_score

    def query(self, x, y, predict=False):
        """
        Compute the nearest neighbor of the input queries.

        Arguments:
            x: A normalized matrix of queries of size (batch_size x key_dim)
            y: A matrix of correct labels (batch_size x 1)
        Returns:
            y_hat, A (batch-size x 1) matrix 
		        - the nearest neighbor to the query in memory_size
            softmax_score, A (batch_size x 1) matrix 
		        - A normalized score measuring the similarity between query and nearest neighbor
            loss - average loss for memory module
        """
        x, y = x.cpu(), y.cpu()
        query = F.normalize(x, dim=1)
        keys_var = ag.Variable(self.keys, requires_grad=False)
        batch_size, dims = query.size()

        # Find the k-nearest neighbors of the query
        scores = torch.matmul(query, torch.t(keys_var))
        cosine_similarity, topk_indices_var = torch.topk(scores, self.top_k, dim=1)

        topk_indices = topk_indices_var.detach().data
        y_hat_indices = topk_indices[:, 0]
        y_hat = self.values[y_hat_indices]

        softmax_score = F.softmax(self.softmax_temperature * cosine_similarity)

        loss = None
        if not predict:
            # Loss Function
            # topk_indices = (batch_size x topk)
            # topk_values =  (batch_size x topk x value_size)

            # collect the memory values corresponding to the topk scores
            flat_topk = flatten(topk_indices)
            flat_topk_values = self.values[topk_indices]
            topk_values = flat_topk_values.resize_(batch_size, self.top_k)

            correct_mask = torch.eq(topk_values, torch.unsqueeze(y.data, dim=1)).float()
            correct_mask_var = ag.Variable(correct_mask, requires_grad=False)

            pos_score, pos_idx = torch.topk(torch.mul(cosine_similarity, correct_mask_var), 1, dim=1)
            neg_score, neg_idx = torch.topk(torch.mul(cosine_similarity, 1-correct_mask_var), 1, dim=1)

            loss = MemoryLoss(pos_score, neg_score, self.margin)

        # Update memory
        self.update(query, y, y_hat, y_hat_indices) 

        return y_hat, softmax_score, loss

    def update(self, query, y, y_hat, y_hat_indices):
        batch_size, dims = query.size()

        # 1) Untouched: Increment memory by 1
        self.age += 1

        # Divide batch by correctness
        result = torch.squeeze(torch.eq(y_hat, torch.unsqueeze(y.data, dim=1))).float()
        incorrect_examples = torch.squeeze(torch.nonzero(1-result))
        correct_examples = torch.squeeze(torch.nonzero(result))

        incorrect = len(incorrect_examples.size()) > 0
        correct = len(correct_examples.size()) > 0

        # 2) Correct: if V[n1] = v
        # Update Key k[n1] <- normalize(q + K[n1]), Reset Age A[n1] <- 0
        if correct:
            correct_indices = y_hat_indices[correct_examples]
            correct_keys = self.keys[correct_indices]
            correct_query = query.data[correct_examples]
            new_correct_keys = F.normalize(correct_keys + correct_query, dim=1)
            self.keys[correct_indices] = new_correct_keys
            self.age[correct_indices] = 0

        # 3) Incorrect: if V[n1] != v
        # Select item with oldest age, Add random offset - n' = argmax_i(A[i]) + r_i 
        # K[n'] <- q, V[n'] <- v, A[n'] <- 0
        if incorrect:
            age_with_noise = self.age + random_uniform((self.memory_size, 1), -self.age_noise, self.age_noise)
            topk_values, topk_indices = torch.topk(age_with_noise, batch_size, dim=0)
            oldest_indices = torch.squeeze(topk_indices)
            self.keys[oldest_indices] = query.data
            self.values[oldest_indices] = torch.unsqueeze(y.data, dim=1)
            self.age[oldest_indices] = 0
