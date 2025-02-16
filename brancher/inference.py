"""
Inference
---------
Module description
"""
import warnings
import copy
from abc import ABC, abstractmethod
from collections.abc import Iterable

import numpy as np
from tqdm import tqdm

import torch

from brancher.optimizers import ProbabilisticOptimizer
from brancher.variables import Variable, ProbabilisticModel, Ensemble
from brancher.stochastic_processes import StochasticProcess
from brancher.standard_variables import DeterministicVariable
from brancher.transformations import truncate_model
from brancher.variables import RootVariable
from brancher import gradient_estimators

from brancher.utilities import reassign_samples
from brancher.utilities import zip_dict
from brancher.utilities import sum_from_dim
from brancher.utilities import to_tensor


def perform_inference(joint_model, number_iterations, number_samples = 1,
                      optimizer='Adam', input_values={},
                      inference_method=None,
                      posterior_model=None,
                      sampler_model=None,
                      pretraining_iterations=0,
                      **opt_params): #TODO: input values
    """
    Summary

    Parameters
    ---------
    """
    if isinstance(joint_model, StochasticProcess):
        posterior_submodel = joint_model.active_posterior_submodel
        joint_submodel = joint_model.active_submodel
        if joint_model.posterior_process is not None:
            joint_submodel.set_posterior_model(posterior_submodel)
        joint_model = joint_submodel
    if isinstance(joint_model, Variable):
        joint_model = ProbabilisticModel([joint_model])
    if not inference_method:
        warnings.warn("The inference method was not specified, using the default reverse KL variational inference")
        inference_method = ReverseKL()
    if posterior_model is None and joint_model.posterior_model is not None:
        posterior_model = joint_model.posterior_model
    if posterior_model is None:
        posterior_model = inference_method.construct_posterior_model(joint_model)
    if not sampler_model: #TODO: clean up
        if not sampler_model:
            try:
                sampler_model = inference_method.sampler_model
            except AttributeError:
                try:
                    sampler_model = joint_model.posterior_sampler
                except AttributeError:
                    sampler_model = None

    joint_model.update_observed_submodel()

    def append_prob_optimizer(model, optimizer, **opt_params):
        prob_opt = ProbabilisticOptimizer(model, optimizer, **opt_params) # TODO: this should be better! handling models with no params
        if prob_opt.optimizer:
            optimizers_list.append(prob_opt)

    optimizers_list = []
    if inference_method.learnable_posterior:
        append_prob_optimizer(posterior_model, optimizer, **opt_params)
    if inference_method.learnable_model:
        append_prob_optimizer(joint_model, optimizer, **opt_params)
    if inference_method.learnable_sampler:
        append_prob_optimizer(sampler_model, optimizer, **opt_params)

    loss_list = []

    inference_method.check_model_compatibility(joint_model, posterior_model, sampler_model)

    for iteration in tqdm(range(number_iterations)):
        loss = inference_method.compute_loss(joint_model, posterior_model, sampler_model, number_samples)

        if torch.isfinite(loss.detach()).all().item():
            [opt.zero_grad() for opt in optimizers_list]
            loss.backward()
            inference_method.correct_gradient(joint_model, posterior_model, sampler_model, number_samples)
            optimizers_list[0].update()
            if iteration > pretraining_iterations:
                [opt.update() for opt in optimizers_list[1:]]
            loss_list.append(loss.cpu().detach().numpy().flatten())
        else:
            warnings.warn("Numerical error, skipping sample")
        loss_list.append(loss.cpu().detach().numpy())
    joint_model.diagnostics.update({"loss curve": np.array(loss_list)})

    inference_method.post_process(joint_model) #TODO: this could be implemented with a with block

    if joint_model.posterior_model is None and inference_method.learnable_posterior:
        inference_method.set_posterior_model_after_inference(joint_model, posterior_model, sampler_model)


class InferenceMethod(ABC):

    @abstractmethod
    def check_model_compatibility(self, joint_model, posterior_model, sampler_model):
        pass

    @abstractmethod
    def compute_loss(self, joint_model, posterior_model, sampler_model, number_samples, input_values):
        pass

    @abstractmethod
    def post_process(self, joint_model):
        pass

    def construct_posterior_model(self, joint_model):
        raise ValueError("Automatic construction of the posterior model is not currently implemented for this inference method. Set the posterior model manually")


class ReverseKL(InferenceMethod):

    def __init__(self, gradient_estimator=gradient_estimators.PathwiseDerivativeEstimator):
        self.learnable_posterior = True
        self.learnable_model = True
        self.needs_sampler = False
        self.learnable_sampler = False
        self.gradient_estimator = gradient_estimator

    def check_model_compatibility(self, joint_model, posterior_model, sampler_model):
        pass #TODO: Check differentiability of the model

    def compute_loss(self, joint_model, posterior_model, sampler_model, number_samples, input_values={}):
        loss = -joint_model.estimate_log_model_evidence(number_samples=number_samples, posterior_model=posterior_model,
                                                        method="ELBO", input_values=input_values,
                                                        for_gradient=True, gradient_estimator=self.gradient_estimator)
        return loss

    def correct_gradient(self, joint_model, posterior_model, sampler_model,
                         number_samples, input_values={}):
        pass

    def post_process(self, joint_model):
        pass

    def construct_posterior_model(self, joint_model):
        raise ValueError("The variational model cannot be constructed automatically as the latent submodel does not contains all the variables")

    def set_posterior_model_after_inference(self, joint_model, posterior_model, sampler_model):
        joint_model.set_posterior_model(posterior_model)



class WassersteinVariationalGradientDescent(InferenceMethod):

    def __init__(self, variational_samplers, particles,
                 cost_function=None,
                 deviation_statistics=None,
                 biased=False,
                 number_post_samples=20000,
                 gradient_estimator=gradient_estimators.PathwiseDerivativeEstimator):
        self.gradient_estimator = gradient_estimator
        self.learnable_posterior = True
        self.learnable_model = False #TODO: to implement later
        self.needs_sampler = True
        self.learnable_sampler = True
        self.biased = biased
        self.number_post_samples = number_post_samples
        if cost_function:
            self.cost_function = cost_function
        else:
            self.cost_function = lambda x, y: sum_from_dim((x - y)**2, dim_index=1)
        if deviation_statistics:
            self.deviation_statistics = deviation_statistics
        else:
            self.deviation_statistics = lambda lst: sum(lst)

        def model_statistics(dic):
            num_samples = list(dic.values())[0].shape[0]
            reassigned_particles = [reassign_samples(p._get_sample(num_samples), source_model=p, target_model=dic)
                                    for p in particles]

            statistics = [self.deviation_statistics([self.cost_function(value_pair[0].detach().cpu().numpy(),
                                                                        value_pair[1].detach().cpu().numpy())
                                                     for var, value_pair in zip_dict(dic, p).items()])
                          for p in reassigned_particles]
            return np.array(statistics).transpose()

        truncation_rules = [lambda a, idx=index: True if (idx == np.argmin(a)) else False
                            for index in range(len(particles))]

        self.sampler_model = [truncate_model(model=sampler,
                                             truncation_rule=rule,
                                             model_statistics=model_statistics)
                              for sampler, rule in zip(variational_samplers, truncation_rules)]

    def check_model_compatibility(self, joint_model, posterior_model, sampler_model):
        assert isinstance(sampler_model, Iterable) and all([isinstance(subsampler, (Variable, ProbabilisticModel))
                                                            for subsampler in sampler_model]), "The Wasserstein Variational GD method require a list of variables or probabilistic models as sampler"
        # TODO: Check differentiability of the model
        # TODO: check particles

    def compute_loss(self, joint_model, posterior_model, sampler_model, number_samples, input_values={}):
        sampler_loss = sum([-joint_model.estimate_log_model_evidence(number_samples=number_samples, posterior_model=subsampler,
                                                                     method="ELBO", input_values=input_values,
                                                                     for_gradient=True, gradient_estimator=self.gradient_estimator)
                            for subsampler in sampler_model])
        particle_loss = self.get_particle_loss(joint_model, posterior_model, sampler_model, number_samples,
                                               input_values)
        return sampler_loss + particle_loss

    def get_particle_loss(self, joint_model, particle_list, sampler_model, number_samples, input_values):
        samples_list = [sampler._get_sample(number_samples, input_values=input_values, max_itr=1)
                        for sampler in sampler_model]
        if self.biased:
            importance_weights = [1./number_samples for _ in sampler_model]
        else:
            importance_weights = [joint_model.get_importance_weights(q_samples=samples,
                                                                     q_model=sampler,
                                                                     for_gradient=False).flatten()
                                  for samples, sampler in zip(samples_list, sampler_model)]
        reassigned_samples_list = [reassign_samples(samples, source_model=sampler, target_model=particle)
                                   for samples, sampler, particle in zip(samples_list, sampler_model, particle_list)]
        pair_list = [zip_dict(particle._get_sample(1), samples)
                     for particle, samples in zip(particle_list, reassigned_samples_list)]
        if not self.biased:
            particle_loss = sum([torch.sum(to_tensor(w)*self.deviation_statistics([self.cost_function(value_pair[0], value_pair[1].detach()) #TODO: numpy()
                                                                    for var, value_pair in particle.items()]))
                                 for particle, w in zip(pair_list, importance_weights)])
        else:
            particle_loss = sum([torch.sum(self.deviation_statistics([self.cost_function(value_pair[0], value_pair[1].detach())
                                                                      for var, value_pair in particle.items()]))
                                 for particle in pair_list])
        return particle_loss

    def correct_gradient(self, joint_model, posterior_model, sampler_model, number_samples, input_values={}):
        pass

    def post_process(self, joint_model):
        sample_list = [sampler._get_sample(self.number_post_samples, max_itr=1)
                        for sampler in self.sampler_model]
        log_weights = []
        for sampler, s in zip(self.sampler_model, sample_list):
            _, logZ = joint_model.get_importance_weights(q_samples=s,
                                                         q_model=sampler,
                                                         for_gradient=False,
                                                         give_normalization=True)
            log_weights.append(logZ)
        log_weights = torch.Tensor(log_weights)
        alpha = log_weights.max()
        un_weights = (log_weights - alpha).exp()
        self.weights = (un_weights/un_weights.sum()).detach()
        print(1)
        #joint_model.set_posterior_model(Ensemble(self.sampler_model, self.weights)) #TODO: Work in progress

    def set_posterior_model_after_inference(self, joint_model, posterior_model, sampler_model):
        j_models = [copy.copy(joint_model) for _ in posterior_model]
        [j_model.set_posterior_model(p_model) for j_model, p_model in zip(j_models, sampler_model)]
        joint_model.posterior_model = Ensemble(j_models, weights=self.weights)

class MaximumLikelihood(InferenceMethod):

    def __init__(self):
        self.learnable_posterior = False
        self.learnable_model = True
        self.needs_sampler = False
        self.learnable_sampler = False

    def construct_posterior_model(self, joint_model):
        return None

    def check_model_compatibility(self, joint_model, posterior_model, sampler_model):
        # TODO: Check differentiability of the model
        pass

    def compute_loss(self, joint_model, posterior_model, sampler_model, number_samples, input_values={}):
        empirical_samples = joint_model.observed_submodel._get_sample(1, observed=True)
        loss = -joint_model.calculate_log_probability(empirical_samples, for_gradient=True)
        return loss.sum()

    def correct_gradient(self, joint_model, posterior_model, sampler_model, number_samples, input_values={}):
        pass

    def post_process(self, joint_model):
        pass


class MAP(InferenceMethod):

    def __init__(self):
        self.learnable_posterior = True
        self.learnable_model = True
        self.needs_sampler = False
        self.learnable_sampler = False

    def construct_posterior_model(self, joint_model):
        test_sample = joint_model._get_sample(1, observed=False)
        posterior_model = ProbabilisticModel([DeterministicVariable(value[0, 0, :], variable.name, learnable=True)
                                              for variable, value in test_sample.items()
                                              if (not variable.is_observed) and not isinstance(variable, (DeterministicVariable, RootVariable))])
        return posterior_model

    def check_model_compatibility(self, joint_model, posterior_model, sampler_model):
        # TODO: Check differentiability of the model
        assert all([isinstance(var, (RootVariable, DeterministicVariable)) for var in posterior_model.flatten()])

    def compute_loss(self, joint_model, posterior_model, sampler_model, number_samples, input_values={}):
        empirical_samples = joint_model.observed_submodel._get_sample(1, observed=True)
        variable_values = reassign_samples(posterior_model._get_sample(1), source_model=posterior_model,
                                           target_model=joint_model)
        variable_values.update(empirical_samples)
        loss = -joint_model.calculate_log_probability(variable_values, for_gradient=True)
        return loss.sum()

    def correct_gradient(self, joint_model, posterior_model, sampler_model, number_samples, input_values={}):
        pass

    def post_process(self, joint_model):
        pass


class SteinVariationalGradientDescent(InferenceMethod): #TODO: work in progress

    def __init__(self):
        self.learnable_posterior = True
        self.learnable_model = False  # TODO: to implement later
        self.needs_sampler = False
        self.learnable_sampler = False
        self.deviation = lambda x, y: np.sum(((to_tensor(x) - to_tensor(y))**2).detach().numpy())
        self.kernel = lambda d, bw: np.exp(-d/(2*bw))
        self.bandwidth = 0.01

    def check_model_compatibility(self, joint_model, posterior_model, sampler_model):
        # TODO: Check differentiability of the model
        # TODO; check particles
        pass

    def compute_loss(self, joint_model, posterior_model, sampler_model, number_samples, input_values={}):
        empirical_samples = joint_model.observed_submodel._get_sample(1, observed=True)
        particle_samples = [reassign_samples(particle._get_sample(1), source_model=particle, target_model=joint_model)
                            for particle in posterior_model]
        [sample.update(empirical_samples) for sample in particle_samples]
        loss = sum([-joint_model.calculate_log_probability(sample, for_gradient=True)
                    for sample in particle_samples])
        return loss

    def correct_gradient(self, joint_model, posterior_model, sampler_model, number_samples, input_values={}):
        self.update_bandwidth(posterior_model)
        gradients = [[variable.value.grad for variable in particle.flatten()] for particle in posterior_model]
        kernel_matrix = [[self.kernel(sum([self.deviation(variable1.value, variable2.value)
                                          for variable1, variable2 in zip(particle1.flatten(), particle2.flatten())]),
                                      bw=self.bandwidth)
                          for particle1 in posterior_model] for particle2 in posterior_model]
        interaction_matrix = [[[-(variable1.value - variable2.value)*kernel_matrix[particle_index1][particle_index2]/self.bandwidth
                               for variable1, variable2 in zip(particle1.flatten(), particle2.flatten())]
                              for particle_index1, particle1 in enumerate(posterior_model)]
                              for particle_index2, particle2 in enumerate(posterior_model)]
        for particle_index, particle in enumerate(posterior_model):
            for variable_index, variable in enumerate(particle.flatten()):
                variable.value.grad = sum([kernel_matrix[particle_index][other_index]*other_gradient[variable_index] + interaction_matrix[particle_index][other_index][variable_index]
                                           for other_index, other_gradient in enumerate(gradients)])

    def update_bandwidth(self, posterior_model):
        distances = [np.sqrt(sum([self.deviation(variable1.value, variable2.value)
                                  for variable1, variable2 in zip(particle1.flatten(), particle2.flatten())]))
                     for particle1 in posterior_model
                     for particle2 in posterior_model
                     if particle1 is not particle2]
        bw = 2*np.median(distances)**2/np.log(len(posterior_model))
        self.bandwidth = bw

    def post_process(self, joint_model):
        pass


