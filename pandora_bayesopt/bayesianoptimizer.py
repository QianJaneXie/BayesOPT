import torch
from botorch.fit import fit_gpytorch_model
from gpytorch.mlls import ExactMarginalLogLikelihood
from botorch.utils.sampling import draw_sobol_samples
from botorch.acquisition import ExpectedImprovement
from .acquisition import GittinsIndex, ExpectedImprovementWithCost
from botorch.optim import optimize_acqf
from .utils import fit_gp_model
import matplotlib.pyplot as plt

def plot_posterior(ax,objective_function,model,test_x,train_x,train_y):
    
    with torch.no_grad():
        
        # Plot the objective function at the test points
        ax.plot(test_x.cpu().numpy(), objective_function(test_x.view(-1,1)).numpy(), 'tab:grey', alpha=0.6)
    
        # Calculate the posterior at the test points
        posterior = model.posterior(test_x.unsqueeze(1).unsqueeze(1))

        # Get upper and lower confidence bounds (2 standard deviations from the mean)
        lower, upper = posterior.mvn.confidence_region()
        lower = lower.squeeze(-1).squeeze(-1)
        upper = upper.squeeze(-1).squeeze(-1)
        # Plot training points as black stars
        ax.plot(train_x.cpu().numpy(), train_y.cpu().numpy(), 'k*', alpha=0.8)
        # Plot posterior means as blue line
        ax.plot(test_x.cpu().numpy(), posterior.mean.squeeze(-1).squeeze(-1).cpu().numpy(), alpha=0.8)
        # Shade between the lower and upper confidence bounds
        ax.fill_between(test_x.cpu().numpy(), lower.cpu().numpy(), upper.cpu().numpy(), alpha=0.2)
        
    
    ax.legend(['Objective Function', 'Observed Data', 'Mean', 'Confidence'])

class BayesianOptimizer:
    def __init__(self, objective, dim, maximize, initial_points, input_standardize = False, kernel=None, cost=None):
        self.objective = objective
        self.maximize = maximize
        self.dim = dim
        self.bounds = torch.stack([torch.zeros(dim), torch.ones(dim)])
        self.best_f = None
        self.best_history = []
        self.cost = cost if cost is not None else 1.0
        self.cumulative_cost = 0.0
        self.cost_history = [0.0]
        self.initialize_points(initial_points)
        
        # GP model parameters
        self.input_standardize = input_standardize
        self.kernel = kernel

    def initialize_points(self, initial_points):
        self.x = initial_points
        self.y = self.objective(initial_points)
        self.update_best()

    def update_best(self):
        self.best_f = self.y.max().item() if self.maximize else self.y.min().item()
        self.best_history.append(self.best_f)

    def iterate(self, acquisition_function_class, **acqf_kwargs):
        
        model = fit_gp_model(self.x.detach(), self.y.detach(), input_standardize=self.input_standardize, kernel=self.kernel)
        
        acqf_args = {'model': model, 'maximize': self.maximize}
        
        if acquisition_function_class == ExpectedImprovement:
            acqf_args['best_f'] = self.best_f

        elif acquisition_function_class == ExpectedImprovementWithCost:
            acqf_args['best_f'] = self.best_f
            acqf_args['cost'] = self.cost
            if acqf_kwargs.get('cost_cooling') == True:
                alpha = (self.budget - self.cumulative_cost) / self.budget
                alpha = max(alpha, 0)  # Ensure alpha is non-negative
                acqf_args['alpha'] = alpha

        elif acquisition_function_class == GittinsIndex:
            if acqf_kwargs.get('step_EIpu') == True:
                if self.need_lmbda_update:
                    if callable(self.cost):
                        # Optimize EIpu first to get new_point_EIpu
                        EIpu = ExpectedImprovementWithCost(model=model, best_f=self.best_f, maximize=self.maximize, cost=self.cost)
                        _, new_point_EIpu = optimize_acqf(
                            acq_function=EIpu,
                            bounds=self.bounds,
                            q=1,
                            num_restarts=20*self.dim,
                            raw_samples=1024*self.dim,
                            options={'method': 'L-BFGS-B'},
                        )
                        if self.current_lmbda == None:
                            self.current_lmbda = new_point_EIpu.item() / 2
                        else:
                            self.current_lmbda = min(self.current_lmbda, new_point_EIpu.item() / 2)

                    else:
                        # Optimize EI first to get new_point_EI
                        EI = ExpectedImprovement(model=model, best_f=self.best_f, maximize=self.maximize)
                        _, new_point_EI = optimize_acqf(
                            acq_function=EI,
                            bounds=self.bounds,
                            q=1,
                            num_restarts=20*self.dim,
                            raw_samples=1024*self.dim,
                            options={'method': 'L-BFGS-B'},
                        )
                        if self.current_lmbda == None:
                            self.current_lmbda = new_point_EI.item() / alpha
                        else:
                            self.current_lmbda = min(self.current_lmbda, new_point_EI.item() / alpha)
                    self.need_lmbda_update = False  # Reset the flag
                print("current lambda:", self.current_lmbda)
                acqf_args['lmbda'] = self.current_lmbda
                self.lmbda_history.append(self.current_lmbda)

            elif acqf_kwargs.get('step_halving') == True:
                if self.need_lmbda_update:
                    self.current_lmbda = self.current_lmbda / 2
                    self.need_lmbda_update = False
                acqf_args['lmbda'] = self.current_lmbda
                self.lmbda_history.append(self.current_lmbda)

            else: 
                acqf_args['lmbda'] = acqf_kwargs['lmbda']

            acqf_args['cost'] = self.cost

        else:
            acqf_args.update(**acqf_kwargs)
            
        acq_function = acquisition_function_class(**acqf_args)

        new_point, new_point_acq = optimize_acqf(
            acq_function=acq_function,
            bounds=self.bounds,
            q=1,
            num_restarts=20*self.dim,
            raw_samples=1024*self.dim,
            options={'method': 'L-BFGS-B'},
        )
        self.current_acq = new_point_acq
        new_value = self.objective(new_point)

        self.x = torch.cat((self.x, new_point))
        self.y = torch.cat((self.y, new_value))
        self.update_best()
        self.update_cost(new_point)

        # Check if lmbda needs to be updated in the next iteration
        if acquisition_function_class == GittinsIndex and (acqf_kwargs.get('step_EIpu') == True or acqf_kwargs.get('step_halving') == True):
            if (self.maximize and self.current_acq.item() < self.best_f) or (not self.maximize and -self.current_acq.item() > self.best_f):
                self.need_lmbda_update = True


    def update_cost(self, new_point):
        if callable(self.cost):
            # If self.cost is a function, call it and update cumulative cost
            cost = self.cost(new_point)
            self.cumulative_cost += cost.sum().item()
        else:
            # If self.cost is not a function, just increment cumulative cost by self.cost
            self.cumulative_cost += self.cost

        self.cost_history.append(self.cumulative_cost)

    def print_iteration_info(self, iteration):
        print(f"Iteration {iteration}, New point: {self.x[-1].squeeze().detach().numpy()}, New value: {self.y[-1].item()}")
        print("Best observed value:", self.best_f)
        print("Current acquisition value:", self.current_acq)
        print("Cumulative cost:", self.cumulative_cost)
        if hasattr(self, 'need_lmbda_update'):
            print("Gittins lmbda:", self.lmbda_history[-1])
        print()

    def run(self, num_iterations, acquisition_function_class, **acqf_kwargs):
        self.budget = num_iterations
        if acquisition_function_class == GittinsIndex:
            if acqf_kwargs.get('step_EIpu') == True:
                self.current_lmbda = None
                self.need_lmbda_update = True
                self.lmbda_history = []
            if acqf_kwargs.get('step_halving') == True:
                self.current_lmbda = 0.1
                self.need_lmbda_update = False
                self.lmbda_history = []                

        for i in range(num_iterations):
            self.iterate(acquisition_function_class, **acqf_kwargs)
            self.print_iteration_info(i)

    def run_until_budget(self, budget, acquisition_function_class, **acqf_kwargs):
        self.budget = budget
        if acquisition_function_class == GittinsIndex:
            if acqf_kwargs.get('step_EIpu') == True:
                self.current_lmbda = None
                self.need_lmbda_update = True
                self.lmbda_history = []
            if acqf_kwargs.get('step_halving') == True:
                self.current_lmbda = 0.1
                self.need_lmbda_update = False
                self.lmbda_history = []  

        i = 0
        while self.cumulative_cost < self.budget:
            self.iterate(acquisition_function_class, **acqf_kwargs)
            self.print_iteration_info(i)
            i += 1

    def get_best_value(self):
        return self.best_f

    def get_best_history(self):
        return self.best_history

    def get_cumulative_cost(self):
        return self.cumulative_cost

    def get_cost_history(self):
        return self.cost_history

    def get_regret_history(self, global_optimum):
        """
        Compute the regret history.

        Parameters:
        - global_optimum (float): The global optimum value of the objective function.

        Returns:
        - list: The regret history.
        """
        return [global_optimum - f if self.maximize else f - global_optimum for f in self.best_history]

    def get_lmbda_history(self):
        return self.lmbda_history