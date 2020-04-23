# Copyright Contributors to the Pyro project.
# SPDX-License-Identifier: Apache-2.0

import torch

import pyro
import pyro.distributions as dist
from pyro.ops.tensor_utils import convolve

from .compartmental import CompartmentalModel


class SIRModel(CompartmentalModel):
    """
    Susceptible-Infected-Recovered model.

    :param int population:
    :param float recovery_time:
    :param iterable data: Time series of new observed infections.
    """

    def __init__(self, population, recovery_time, data):
        compartments = ("S", "I")  # R is implicit.
        duration = len(data)
        super().__init__(compartments, duration, population)

        assert isinstance(recovery_time, float)
        assert recovery_time > 0
        self.recovery_time = recovery_time

        self.data = data

    series = ("S2I", "I2R")
    full_mass = [("R0", "rho")]

    def heuristic(self):
        # Start with a single infection.
        S0 = self.population - 1
        # Assume 50% <= response rate <= 100%.
        S2I = self.data * min(2., (S0 / self.data.sum()).sqrt())
        S_aux = (S0 - S2I.cumsum(-1)).clamp(min=0.5)
        # Account for the single initial infection.
        S2I[0] += 1
        # Assume infection lasts less than a month.
        recovery = torch.arange(30.).div(self.recovery_time).neg().exp()
        I_aux = convolve(S2I, recovery)[:len(self.data)].clamp(min=0.5)
        return {
            "R0": torch.tensor(2.0),
            "rho": torch.tensor(0.5),
            "auxiliary": torch.stack([S_aux, I_aux]),
        }

    def global_model(self):
        tau = self.recovery_time
        R0 = pyro.sample("R0", dist.LogNormal(0., 1.))
        rho = pyro.sample("rho", dist.Uniform(0, 1))

        # Convert interpretable parameters to distribution parameters.
        rate_s = -R0 / (tau * self.population)
        prob_i = 1 / (1 + tau)

        return rate_s, prob_i, rho

    def initialize(self, params):
        # Start with a single infection.
        return {"S": self.population - 1, "I": 1}

    def transition_fwd(self, params, state, t):
        rate_s, prob_i, rho = params

        # Compute state update.
        prob_s = -(rate_s * state["I"]).expm1()
        S2I = pyro.sample("S2I_{}".format(t),
                          dist.Binomial(state["S"], prob_s))
        I2R = pyro.sample("I2R_{}".format(t),
                          dist.Binomial(state["I"], prob_i))
        state["S"] = state["S"] - S2I
        state["I"] = state["I"] + S2I - I2R

        # Condition on observations.
        pyro.sample("obs_{}".format(t),
                    dist.ExtendedBinomial(S2I, rho),
                    obs=self.data[t] if t < self.duration else None)

    def transition_bwd(self, params, prev, curr, t):
        rate_s, prob_i, rho = params
        obs = self.data[t]

        # Reverse the S2I,I2R computation.
        S2I = prev["S"] - curr["S"]
        I2R = prev["I"] - curr["I"] + S2I

        # Compute probability factors.
        prob_s = -(rate_s * prev["I"]).expm1()
        S2I_logp = dist.ExtendedBinomial(prev["S"], prob_s).log_prob(S2I)
        I2R_logp = dist.ExtendedBinomial(prev["I"], prob_i).log_prob(I2R)
        obs_logp = dist.ExtendedBinomial(S2I.clamp(min=0), rho).log_prob(obs)
        return obs_logp + S2I_logp + I2R_logp