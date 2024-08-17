#!/usr/bin/env python3

import torch
from botorch.models import SingleTaskGP
from gpytorch.likelihoods import FixedNoiseGaussianLikelihood
from gpytorch.kernels import MaternKernel, RBFKernel, ScaleKernel
from botorch.utils.sampling import draw_sobol_samples
from botorch.sampling.pathwise import draw_kernel_feature_paths
from botorch.utils.sampling import optimize_posterior_samples
from botorch.acquisition import ExpectedImprovement, UpperConfidenceBound
from pandora_bayesopt.acquisition.multi_step_ei import MultiStepLookaheadEI
from botorch.acquisition.knowledge_gradient import qKnowledgeGradient
from botorch.acquisition.predictive_entropy_search import qPredictiveEntropySearch
from botorch.acquisition.max_value_entropy_search import qMaxValueEntropy
from pandora_bayesopt.acquisition.gittins import GittinsIndex
from pandora_bayesopt.bayesianoptimizer import BayesianOptimizer

import numpy as np
import wandb

# use a GPU if available
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# Set default tensor type to float64
torch.set_default_dtype(torch.float64)

def run_bayesopt_experiment(config):
    print(config)

    dim = config['dim']
    lengthscale = config['lengthscale']
    outputscale = config['amplitude']
    maximize = True
    kernel = config['kernel']
    if kernel == 'Matern32':
        nu = 1.5
        if lengthscale == 1.0: 
            num_iterations = 10*dim
        elif lengthscale == 0.5:
            num_iterations = 15*dim
        elif lengthscale == 0.1:
            num_iterations = 25*dim
    elif kernel == 'Matern52':
        nu = 2.5
        if lengthscale == 1.0: 
            num_iterations = 10*dim
        elif lengthscale == 0.5:
            num_iterations = 15*dim
        elif lengthscale == 0.1:
            num_iterations = 25*dim
    elif kernel == 'RBF':
        if lengthscale == 1.0: 
            num_iterations = 5*dim
        elif lengthscale == 0.5:
            num_iterations = 10*dim 
        elif lengthscale == 0.1:
            num_iterations = 20*dim
    seed = config['seed']
    torch.manual_seed(seed)
    
    # Create the objective function
    if kernel == 'RBF':
        base_kernel = RBFKernel().double()
    else:
        base_kernel = MaternKernel(nu=nu).double()
    base_kernel.lengthscale = torch.tensor([[lengthscale]])
    scale_kernel = ScaleKernel(base_kernel).double()
    scale_kernel.outputscale = torch.tensor([[outputscale]])

    # Define Noise Level
    noise_level = 1e-4

    # Initialize Placeholder Data with Correct Dimensions
    num_samples = 1  # Replace with actual number of samples
    num_features = dim  # Replace with actual number of features
    train_X = torch.zeros(num_samples, num_features)  # Placeholder data
    train_Y = torch.zeros(num_samples, 1)             # Placeholder data
    Yvar = torch.ones(num_samples) * noise_level

    # Initialize Model
    model = SingleTaskGP(train_X, train_Y, likelihood = FixedNoiseGaussianLikelihood(noise=Yvar), covar_module=scale_kernel)

    # Draw a sample path
    sample_path = draw_kernel_feature_paths(model, sample_shape=torch.Size([1]))
    def objective_function(x):
        return sample_path(x).squeeze(0).detach()

    # Find the global optimum
    bounds = torch.stack([torch.zeros(dim), torch.ones(dim)])
    global_optimum_point, global_optimum_value = optimize_posterior_samples(paths=sample_path, bounds=bounds, raw_samples=1024*dim, num_restarts=20*dim, maximize=maximize)
    print("global optimum point:", global_optimum_point.detach().numpy())
    print("global optimum value:", global_optimum_value.item())

    # Set up the kernel
    if kernel == 'RBF':
        base_kernel = RBFKernel().double()
    else:
        base_kernel = MaternKernel(nu=nu).double()
    base_kernel.lengthscale = lengthscale
    base_kernel.raw_lengthscale.requires_grad = False
    scale_kernel = ScaleKernel(base_kernel).double()
    scale_kernel.outputscale = torch.tensor([[outputscale]])
    scale_kernel.raw_outputscale.requires_grad = False

    # Test performance of different policies
    draw_initial_method = config['draw_initial_method']
    if draw_initial_method == 'sobol':
        bounds = torch.stack([torch.zeros(dim), torch.ones(dim)])
        init_x = draw_sobol_samples(bounds=bounds, n=1, q=2*(dim+1)).squeeze(0)
    output_standardize = config['output_standardize']

    policy = config['policy']
    print("policy:", policy)

    Optimizer = BayesianOptimizer(
        dim=dim, 
        maximize=maximize, 
        initial_points=init_x,
        objective=objective_function, 
        kernel=scale_kernel,
        output_standardize=output_standardize
    )
    if policy == 'RandomSearch':
        Optimizer.run(
            num_iterations=num_iterations, 
            acquisition_function_class="RandomSearch"
        )
    elif policy == 'ExpectedImprovement':
        Optimizer.run(
            num_iterations=num_iterations, 
            acquisition_function_class=ExpectedImprovement
        )
    elif policy == 'ThompsonSampling':
        Optimizer.run(
            num_iterations=num_iterations, 
            acquisition_function_class="ThompsonSampling"
        )
    elif policy == 'UpperConfidenceBound':
        Optimizer.run(
            num_iterations=num_iterations, 
            acquisition_function_class=UpperConfidenceBound,
            heuristic=True
        )
    elif policy == 'PredictiveEntropySearch':
        Optimizer.run(
            num_iterations=num_iterations, 
            acquisition_function_class=qPredictiveEntropySearch
        )
    elif policy == 'MaxValueEntropy':
        Optimizer.run(
            num_iterations=num_iterations, 
            acquisition_function_class=qMaxValueEntropy
        )
    elif policy == 'KnowledgeGradient':
        Optimizer.run(
            num_iterations=num_iterations, 
            acquisition_function_class=qKnowledgeGradient
        )
    elif policy == 'MultiStepLookaheadEI':
        Optimizer.run(
            num_iterations=num_iterations, 
            acquisition_function_class=MultiStepLookaheadEI
        )
    elif policy == 'Gittins_Lambda_01':
        Optimizer.run(
            num_iterations=num_iterations, 
            acquisition_function_class = GittinsIndex,
            lmbda = 0.01
        )
    elif policy == 'Gittins_Lambda_001':
        Optimizer.run(
            num_iterations = num_iterations, 
            acquisition_function_class = GittinsIndex,
            lmbda = 0.001
        )
    elif policy == 'Gittins_Lambda_0001':
        Optimizer.run(
            num_iterations = num_iterations, 
            acquisition_function_class = GittinsIndex,
            lmbda = 0.0001
        )
    elif policy == 'GittinsDecay_InitLambda_0001':
        Optimizer.run(
            num_iterations=num_iterations,
            acquisition_function_class=GittinsIndex,
            step_divide = True,
            init_lmbda = 0.0001,
            alpha = 2
        )
    cost_history = Optimizer.get_cost_history()
    best_history = Optimizer.get_best_history()
    regret_history = Optimizer.get_regret_history(global_optimum_value.item())
    acq_history = Optimizer.get_acq_history()

    print("Cost history:", cost_history)
    print("Best history:", best_history)
    print("Regret history:", regret_history)
    print("Acq history:", acq_history)
    print()

    return (global_optimum_value.item(), cost_history, best_history, regret_history, acq_history)

wandb.init()
(global_optimum_value, cost_history, best_history, regret_history, acq_history) = run_bayesopt_experiment(wandb.config)

wandb.log({"global optimum value": global_optimum_value})

for cost, best, regret, acq in zip(cost_history, best_history, regret_history, acq_history):
    wandb.log({"cumulative cost": cost, "best observed": best, "regret": regret, "lg(regret)": np.log10(regret), "acquisition value": acq})

wandb.finish()