# coding=utf-8
from __future__ import print_function

import theano
from theano import tensor
from theano.tensor import slinalg
from theano.tensor import nlinalg
from theano.sandbox.rng_mrg import MRG_RandomStreams as RandomStreams
from theano.ifelse import ifelse

import numpy as np
from utils import Container, tt

pi = 3.141592653
trng = RandomStreams(1234)

def log_normal(x, prec):
    return -0.5 * (tensor.log(2*pi / prec) + prec * tensor.sqr(x))

def log_prior_normal(model_params, prec):
    res = 0.
    for p in model_params:
        res += tensor.sum(log_normal(p, prec))
    return res

def logpdf_normal(pp, yy, prec_lik):
    logliks = - 0.5 * (np.log(2 * pi / prec_lik) + prec_lik * (pp - yy)**2)
    return logliks

def compute_rmse(xx, yy):
    return np.sqrt(((xx - yy)**2).mean())

def grad_clipping(model_params, grads, gc_norm = 10.):
    norm = 0.
    for g in grads:
        norm = norm + tensor.sum(tensor.sqr(g))
    sqrtnorm = tensor.sqrt(norm)
    adj_norm_gs = tensor.switch(tensor.ge(sqrtnorm, gc_norm),
                           gc_norm / sqrtnorm, 1.)
    return adj_norm_gs

class Trainer(object):
    '''Abstract base class for all SG-MCMC trainers.
    This is NOT a trainer in itself.
    '''

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.updates = []
        self.params = {}
        self.inputs = None
        self.model_outputs = None
        self.true_outputs = tensor.vector('true_outputs')

    def initialize_params(self, params, data):
        self.params['batch_size'] = params['batch_size'] if 'batch_size' in params else 32
        self.params['burn_in'] = params['burn_in'] if 'burn_in' in params else 10000
        self.params['n_iter'] = params['n_iter'] if 'n_iter' in params else 500000
        self.params['print_iter'] = params['print_iter'] if 'print_iter' in params else 100
        self.params['prec_lik'] = params['prec_lik'] if 'prec_lik' in params else 1.25
        self.params['prec_prior'] = params['prec_prior'] if 'prec_prior' in params else 1.
        self.params['lr_decay'] = params['lr_decay'] if 'lr_decay' in params else 80000
        self.params['thinning'] = params['thinning'] if 'thinning' in params else 10
        self.params['gc_norm'] = params['gc_norm'] if 'gc_norm' in params else 1.0
        self.params['train_size'] = data.x_train.shape[0]
        self.params['val_size'] = data.x_val.shape[0]

    def _create_auxiliary_variables(self):
        self.lr = tensor.scalar('lr')

    def _get_updates(self):
        raise NotImplementedError

    def _get_training_function(self):
        return theano.function(inputs  = [self.inputs, self.true_outputs,
                                          self.lr],
                               outputs = [self.model_outputs, self.sumloglik],
                               updates = self.updates,
                               allow_input_downcast = True)

    def _get_prediction_function(self):
        return theano.function(inputs  = [self.inputs],
                               outputs = self.model_outputs,
                               allow_input_downcast = True)

    def train(self, model, data, params = {}):
        '''Main training loop.

        Arguments:
            model: object / Container (see utils.py)
                   The model. It should have at least the following attributes:
                   model.inputs: theano.tensor.matrix
                                 Representing input minibatch.
                   model.outputs: theano.tensor.row
                                  Representing model outputs.
                   model.weights: list of theano.shared variables
                                  List of model parameters.

            data: object / Container
                  The data. It should have at least the following attributes:
                  data.x_train: np.array
                  data.y_train: np.array
                  data.x_val: np.array
                  data.y_val: np.array

            params: dict
                    Additional parameters for training.
        '''

        self.inputs = model.inputs
        self.model_outputs = model.outputs
        self.weights = model.weights
        self.initialize_params(params, data)
        self._create_auxiliary_variables()

        # get update equations
        self.updates, self.sumloglik = self._get_updates()

        n = self.params['batch_size']
        N = self.params['train_size']
        lr = self.params['lr']

        # create training and prediction functions
        fn = Container()
        fn.train = self._get_training_function()
        fn.predict = self._get_prediction_function()

        avg_pp = np.zeros(data.y_val.shape)
        sum_pp = np.zeros(data.y_val.shape)
        sumsqr_pp = np.zeros(data.y_val.shape)
        n_samples = 0
        do_sampling = False

        for i in range(self.params['n_iter']):

            # prepare next minibatch
            mini_idx = np.floor(np.random.rand(n) * N).astype('int32')
            mini_X = data.x_train[mini_idx]
            mini_Y = data.y_train[mini_idx]

            # parameter update
            train_pp, sumloglik = fn.train(mini_X, mini_Y, lr)

            if i % self.params['lr_decay'] == 0:
                lr /= 2

            if i == self.params['burn_in']:
                # burnin period over, begin sampling
                do_sampling = True

            if i % self.params['thinning'] == 0:
                val_pp = fn.predict(data.x_val)

                if do_sampling:
                    n_samples += 1
                    # prediction based on current parameter (sample)
                    avg_pp = ((1 - (1./n_samples)) * avg_pp) + ((1./n_samples) * val_pp)
                    # trn_pp = fn.predict(data.x_train) # train predictions
                    ppp = avg_pp
                    # online sample variance
                    sum_pp += val_pp
                    sumsqr_pp += val_pp * val_pp
                    var_pp = (sumsqr_pp - (sum_pp * sum_pp)/n_samples) / (n_samples - 1)
                else:
                    ppp = val_pp

                    trn_pp = fn.predict(data.x_train) # train predictions
                    var_pp = np.var(trn_pp - data.y_train)

                meanloglik = logpdf_normal(ppp, data.y_val, 1/var_pp).mean()
                rmse = compute_rmse(ppp, data.y_val)
                # trmse = compute_rmse(trn_pp, data.y_train)

                print ('%d/%d, %.2f, %.2f (%.2f)  \n' % \
                        (i, n_samples, sumloglik, meanloglik, rmse), end = "")

        print ('%d/%d, %.2f, %.2f (%.2f)' % \
              (i, n_samples, sumloglik, meanloglik, rmse))


class SGLD(Trainer):
    '''Stochastic Gradient Langevin Dynamics
    Implemented according to the paper:
    Welling, Max, and Yee W. Teh., 2011
    "Bayesian learning via stochastic gradient Langevin dynamics."

    Arguments:
        lr: float.
            The initial learning rate.
    '''

    def __init__(self, initial_lr=1.0e-5, **kwargs):
        super(SGLD, self).__init__(**kwargs)
        self.params['lr'] = initial_lr

    def initialize_params(self, params, data):
        self.params['batch_size'] = params['batch_size'] if 'batch_size' in params else 32
        self.params['burn_in'] = params['burn_in'] if 'burn_in' in params else 10000
        self.params['n_iter'] = params['n_iter'] if 'n_iter' in params else 500000
        self.params['print_iter'] = params['print_iter'] if 'print_iter' in params else 100
        self.params['prec_lik'] = params['prec_lik'] if 'prec_lik' in params else 1.25
        self.params['prec_prior'] = params['prec_prior'] if 'prec_prior' in params else 1.
        self.params['lr_decay'] = params['lr_decay'] if 'lr_decay' in params else 80000
        self.params['thinning'] = params['thinning'] if 'thinning' in params else 10
        self.params['gc_norm'] = params['gc_norm'] if 'gc_norm' in params else None
        self.params['train_size'] = data.x_train.shape[0]
        self.params['val_size'] = data.x_val.shape[0]

    def _get_updates(self):
        n = self.params['batch_size']
        N = self.params['train_size']
        prec_lik = self.params['prec_lik']
        prec_prior = self.params['prec_prior']
        gc_norm = self.params['gc_norm']

        # compute log-likelihood
        error = self.model_outputs - self.true_outputs
        logliks = log_normal(error, prec_lik)
        sumloglik = logliks.sum()
        logprior = log_prior_normal(self.weights, prec_prior)
        logpost = N * sumloglik / n + logprior

        #compute gradients
        grads = tensor.grad(cost = logpost,  wrt = self.weights)

        # gradient clipping 
        if gc_norm is not None:
            adj_norm_gs = grad_clipping(self.weights, grads, gc_norm)  
        else:
            adj_norm_gs = 1.0

        updates = []
        for p, g in zip(self.weights, grads):
            grad = g * adj_norm_gs
            #inject noise
            noise = tensor.sqrt(self.lr) * trng.normal(p.shape)
            updates.append((p, p + 0.5 * self.lr * grad + noise))

        return updates, sumloglik

class SGFS(Trainer):
    '''Stochastic Gradient Fisher Scoring
    Implemented according to the paper:
    Ahn, Sungjin et. al., 2012
    "Bayesian posterior sampling via stochastic gradient Fisher scoring."

    Arguments:
        initial_lr: float.
                    The initial learning rate.
        B: numpy.array of size(n_weights, n_weights).
           Symmetric positive-definite matrix. Here n_weights is the total
           number of parameters in the model
    '''

    def __init__(self, initial_lr=1.0e-5, B=None, **kwargs):
        super(SGFS, self).__init__(**kwargs)
        self.params['lr'] = initial_lr
        if B:
            self.params['B'] = B

    def _create_auxiliary_variables(self):
        self.lr = tensor.scalar('lr')
        self.n_weights = 0
        for p in self.weights:
            self.n_weights += np.prod(p.get_value().shape)
        self.I_t = theano.shared(np.asarray(np.zeros((self.n_weights, self.n_weights)),
                                            dtype = theano.config.floatX))
        self.it = theano.shared(1.)

    def _get_updates(self):
        n = self.params['batch_size']
        N = self.params['train_size']
        prec_lik = self.params['prec_lik']
        prec_prior = self.params['prec_prior']
        gc_norm = self.params['gc_norm']
        gamma = float(n + N) / n

        # compute log-likelihood
        error = self.model_outputs - self.true_outputs
        logliks = log_normal(error, prec_lik)
        sumloglik = logliks.sum()

        # compute gradient of likelihood wrt each data point
        grads = tensor.jacobian(expression = logliks, wrt = self.weights)
        grads = tensor.concatenate([g.flatten(ndim=2) for g in grads], axis=1)
        avg_grads = grads.mean(axis = 0)
        dist_grads = grads - avg_grads

        # compute variance of gradient
        var_grads = (1. / (n-1)) * tensor.dot(dist_grads.T, dist_grads)

        logprior = log_prior_normal(self.weights, prec_prior)
        grads_prior = tensor.grad(cost = logprior, wrt = self.weights)
        grads_prior = tensor.concatenate([g.flatten() for g in grads_prior])

        # update Fisher information
        I_t_next = (1 - 1/self.it) * self.I_t + 1/self.it * var_grads

        # compute noise
        if 'B' in self.params:
            B = self.params['B']
        else:
            B = gamma * I_t_next * N
        # B += np.eye(self.n_weights) * (10 ** -9)
        B_ch = slinalg.cholesky(B)
        noise = tensor.dot(((2./tensor.sqrt(self.lr)) * B_ch),
                           trng.normal((self.n_weights, 1)))

        # expensive inversion
        inv_cond_mat = gamma * N * I_t_next + (4./self.lr) * B
        cond_mat = nlinalg.matrix_inverse(inv_cond_mat)

        updates = []
        updates.append((self.I_t, I_t_next))
        updates.append((self.it, self.it + 1))

        # update the parameters
        updated_params = 2 * tensor.dot(cond_mat, grads_prior + N * avg_grads + noise.flatten())
        updated_params = updated_params.flatten()
        last_row = 0
        for p in self.weights:
            sub_index = np.prod(p.get_value().shape)
            up = updated_params[last_row:last_row+sub_index]
            up = up.reshape(p.shape)
            updates.append((p, up))
            last_row += sub_index

        return updates, sumloglik

class SGNHT(Trainer):
    '''Stochastic Gradient Nosé-Hoover Thermostat
    Implemented according to the paper:
    Ding, Nan, et al., 2014
    "Bayesian Sampling Using Stochastic Gradient Thermostats."

    Arguments:
        initial_lr: float.
                    The initial learning rate.
        A: float.
           Diffusion parameter.
    '''

    def __init__(self, initial_lr=1.0e-5, A = 1., **kwargs):
        super(SGNHT, self).__init__(**kwargs)
        self.params['lr'] = initial_lr
        self.params['A'] = A

    def _create_auxiliary_variables(self):
        self.lr = tensor.scalar('lr')
        self.velocities = [theano.shared(np.asarray(np.random.randn(*p.get_value().shape),
                                                    dtype = theano.config.floatX))
                           for p in self.weights]
        self.kinetic_energy = theano.shared(self.params['A'])

    def _get_updates(self):
        n = self.params['batch_size']
        N = self.params['train_size']
        prec_lik = self.params['prec_lik']
        prec_prior = self.params['prec_prior']
        gc_norm = self.params['gc_norm']

        # compute log-likelihood
        error = self.model_outputs - self.true_outputs
        logliks = log_normal(error, prec_lik)
        sumloglik = logliks.sum()
        logprior = log_prior_normal(self.weights, prec_prior)
        logpost = N * sumloglik / n + logprior

        # compute gradients
        grads = tensor.grad(cost = logpost, wrt = self.weights)

        updates = []
        new_kinetic_energy = 0.
        for p, g, v in zip(self.weights, grads, self.velocities):
            #inject noise
            noise = tensor.sqrt(self.lr * self.params['A']) * trng.normal(p.shape)
            new_v = v - self.kinetic_energy * self.lr * v + self.lr * g + noise
            updates.append((v, new_v))
            updates.append((p, p + self.lr * new_v))
            new_kinetic_energy += tensor.sum(tensor.sqr(new_v))

        updates.append((self.kinetic_energy,
                       self.kinetic_energy + ((new_kinetic_energy/n) - 1) * self.lr))

        return updates, sumloglik

class mSGNHT(Trainer):
    '''Multivariable Stochastic Gradient Nosé-Hoover Thermostat
    Implemented according to the paper:
    Gan et al., 2015
    "Scalable Deep Poisson Factor Analysis for Topic Modeling"

    Arguments:
        initial_lr: float.
                    The initial learning rate.
        A: np.array.
           Diffusion matrix
    '''

    def __init__(self, initial_lr=1.0e-5, A=1.0, **kwargs):
        super(mSGNHT, self).__init__(**kwargs)
        self.params['lr'] = initial_lr
        if A:
            self.params['A'] = A

    def _create_auxiliary_variables(self):
        self.lr = tensor.scalar('lr')
        self.velocities = [theano.shared(np.asarray(np.random.randn(*p.get_value().shape),
                                                    dtype = theano.config.floatX))
                           for p in self.weights]
        self.kinetic_energies = [theano.shared(self.params['A'] * p.get_value())
                                 for p in self.weights]

    def _get_updates(self):
        n = self.params['batch_size']
        N = self.params['train_size']
        prec_lik = self.params['prec_lik']
        prec_prior = self.params['prec_prior']
        gc_norm = self.params['gc_norm']

        # compute log-likelihood
        error = self.model_outputs - self.true_outputs
        logliks = log_normal(error, prec_lik)
        sumloglik = logliks.sum()
        logprior = log_prior_normal(self.weights, prec_prior)
        logpost = N * sumloglik / n + logprior

        # compute gradients
        grads = tensor.grad(cost = logpost, wrt = self.weights)

        updates = []
        for p, g, v, k in zip(self.weights, grads, self.velocities, self.kinetic_energies):
            #inject noise
            noise = tensor.sqrt(self.lr * self.params['A']) * trng.normal(p.shape)
            new_v = v - k * self.lr * v + self.lr * g + noise
            updates.append((v, new_v))
            updates.append((p, p + self.lr * new_v))
            updates.append((k, k + self.lr * (tensor.sqr(new_v) - 1.)))

        return updates, sumloglik

class pSGLD(Trainer):
    '''Preconditioned Stochastic Gradient Langevin Dynamics
    Implemented according to the paper:
    Li, Chunyuan, et al., 2015
    "Preconditioned stochastic gradient Langevin dynamics
    for deep neural networks."

    Arguments:
        initial_lr: float.
                    The initial learning rate
        alpha: float.
               Balances current vs. historic gradient
        mu: float.
            Controls curvature of preconditioning matrix
            (Corresponds to lambda in the paper)
        use_gamma: boolean.
                   Whether to use the Gamma term which is expensive to compute
    '''

    def __init__(self, initial_lr=1.0e-5, alpha=0.99, mu=1.0e-5, use_gamma = False, **kwargs):
        super(pSGLD, self).__init__(**kwargs)
        self.params['lr'] = initial_lr
        self.params['mu'] = mu
        self.params['alpha'] = alpha
        self.params['use_gamma'] = use_gamma

    def _create_auxiliary_variables(self):
        self.lr = tensor.scalar('lr')
        self.V_t = [theano.shared(np.asarray(np.zeros(p.get_value().shape),
                                             dtype = theano.config.floatX))
                    for p in self.weights]


    def _get_updates(self):
        n = self.params['batch_size']
        N = self.params['train_size']
        prec_lik = self.params['prec_lik']
        prec_prior = self.params['prec_prior']
        gc_norm = self.params['gc_norm']
        alpha = self.params['alpha']
        mu = self.params['mu']
        use_gamma = self.params['use_gamma']

        # compute log-likelihood
        error = self.model_outputs - self.true_outputs
        logliks = log_normal(error, prec_lik)
        sumloglik = logliks.sum()
        meanloglik = sumloglik / n

        # compute gradients
        grads = tensor.grad(cost = meanloglik, wrt = self.weights)

        # update preconditioning matrix
        V_t_next = [alpha * v + (1 - alpha) * g * g for g, v in zip(grads, self.V_t)]
        G_t = [1. / (mu + tensor.sqrt(v)) for v in V_t_next]

        logprior = log_prior_normal(self.weights, prec_prior)
        grads_prior = tensor.grad(cost = logprior, wrt = self.weights)

        updates = []
        [updates.append((v, v_n)) for v, v_n in zip(self.V_t, V_t_next)]

        for p, g, gp, gt in zip(self.weights, grads, grads_prior, G_t):
            # inject noise
            noise = tensor.sqrt(self.lr * gt) * trng.normal(p.shape)
            if use_gamma:
                # compute gamma
                gamma = nlinalg.extract_diag(tensor.jacobian(gt.flatten(), p).flatten(ndim=2))
                gamma = gamma.reshape(p.shape)
                updates.append((p, p + 0.5 * self.lr * ((gt * (gp + N * g)) + gamma) + noise))
            else:
                updates.append((p, p + 0.5 * self.lr * (gt * (gp + N * g)) + noise))

        return updates, sumloglik
