"""
Defines generic PyMC ModelBuilder class and subclasses for
* WeightedSumFitter model for Synthetic Control experiments
* LinearRegression model
"""
from typing import Any, Dict, Optional

import arviz as az
import numpy as np
import pandas as pd
import pymc as pm
import pytensor.tensor as pt
from arviz import r2_score


class ModelBuilder(pm.Model):
    """
    This is a wrapper around pm.Model to give scikit-learn like API.

    Public Methods
    --------
    * build_model: must be implemented by subclasses
    * fit: populates idata attribute
    * predict: returns predictions on new data
    * score: returns Bayesian R^2
    """

    def __init__(self, sample_kwargs: Optional[Dict[str, Any]] = None):
        """
        :param sample_kwargs: A dictionary of kwargs that get unpacked and passed to the
            :func:`pymc.sample` function. Defaults to an empty dictionary.
        """
        super().__init__()
        self.idata = None
        self.sample_kwargs = sample_kwargs if sample_kwargs is not None else {}

    def build_model(self, X, y, coords) -> None:
        """Build the model.

        Example
        -------
        >>> class CausalPyModel(ModelBuilder):
        >>>    def build_model(self, X, y):
        >>>        with self:
        >>>            X_ = pm.MutableData(name="X", value=X)
        >>>            y_ = pm.MutableData(name="y", value=y)
        >>>            beta = pm.Normal("beta", mu=0, sigma=1, shape=X_.shape[1])
        >>>            sigma = pm.HalfNormal("sigma", sigma=1)
        >>>            mu = pm.Deterministic("mu", pm.math.dot(X_, beta))
        >>>            pm.Normal("y_hat", mu=mu, sigma=sigma, observed=y_)
        """
        raise NotImplementedError("This method must be implemented by a subclass")

    def _data_setter(self, X) -> None:
        """
        Set data for the model.

        This method is used internally to register new data for the model for
        prediction.
        """
        with self.model:
            pm.set_data({"X": X})

    def fit(self, X, y, coords: Optional[Dict[str, Any]] = None) -> None:
        """Draw samples fromposterior, prior predictive, and posterior predictive
        distributions, placing them in the model's idata attribute.

        Example
        -------
        >>> import numpy as np
        >>> import pymc as pm
        >>> from causalpy.pymc_models import ModelBuilder
        >>> class MyToyModel(ModelBuilder):
        ...    def build_model(self, X, y, coords):
        ...        with self:
        ...            X_ = pm.MutableData(name="X", value=X)
        ...            y_ = pm.MutableData(name="y", value=y)
        ...            beta = pm.Normal("beta", mu=0, sigma=1, shape=X_.shape[1])
        ...            sigma = pm.HalfNormal("sigma", sigma=1)
        ...            mu = pm.Deterministic("mu", pm.math.dot(X_, beta))
        ...            pm.Normal("y_hat", mu=mu, sigma=sigma, observed=y_)

        >>> rng = np.random.default_rng(seed=42)
        >>> X = rng.normal(loc=0, scale=1, size=(20, 2))
        >>> y = rng.normal(loc=0, scale=1, size=(20,))
        >>> model = MyToyModel(sample_kwargs={"chains": 2, "draws": 2})
        >>> model.fit(X, y)
        Only 2 samples in chain.
        Auto-assigning NUTS sampler...
        Initializing NUTS using jitter+adapt_diag...
        Multiprocess sampling (2 chains in 4 jobs)
        NUTS: [beta, sigma]
        Sampling 2 chains for 1_000 tune and 2 draw iterations (2_000 + 4 draws total)
          took 0 seconds.gences]
        The number of samples is too small to check convergence reliably.
        Sampling: [beta, sigma, y_hat]
        Sampling: [y_hat]
        Inference data with groups:
                > posterior
                > posterior_predictive
                > sample_stats
                > prior
                > prior_predictive
                > observed_data
                > constant_data
        """
        self.build_model(X, y, coords)
        with self.model:
            self.idata = pm.sample(**self.sample_kwargs)
            self.idata.extend(pm.sample_prior_predictive())
            self.idata.extend(
                pm.sample_posterior_predictive(self.idata, progressbar=False)
            )
        return self.idata

    def predict(self, X):
        """
        Predict data given input data `X`

        Results in KeyError if model hasn't been fit.

        Example
        -------
        # Assumes `model` has been initialized and .fit() has been run,
        # see ModelBuilder().fit() for example

        >>> X_new = rng.normal(loc=0, scale=1, size=(20,2))
        >>> model.predict(X_new)
        Sampling: [beta, y_hat]
        Inference data with groups:
                > posterior_predictive
                > observed_data
                > constant_data
        """

        self._data_setter(X)
        with self.model:  # sample with new input data
            post_pred = pm.sample_posterior_predictive(
                self.idata, var_names=["y_hat", "mu"], progressbar=False
            )
        return post_pred

    def score(self, X, y) -> pd.Series:
        """Score the Bayesian :math:`R^2` given inputs ``X`` and outputs ``y``.

        .. caution::

            The Bayesian :math:`R^2` is not the same as the traditional coefficient of
            determination, https://en.wikipedia.org/wiki/Coefficient_of_determination.

        Example
        --------
        # Assuming `model` has been fit
        >>> model.score(X, y) # X, y are random data here
        Sampling: [y_hat]
        r2        0.352251
        r2_std    0.051624
        dtype: float64
        """
        yhat = self.predict(X)
        yhat = az.extract(
            yhat, group="posterior_predictive", var_names="y_hat"
        ).T.values
        # Note: First argument must be a 1D array
        return r2_score(y.flatten(), yhat)

    # .stack(sample=("chain", "draw")


class WeightedSumFitter(ModelBuilder):
    """
    Used for synthetic control experiments

    Defines model:
    y ~ Normal(mu, sigma)
    sigma ~ HalfNormal(1)
    mu = X * beta
    beta ~ Dirichlet(1,...,1)

    Public Methods
    ---------------
    * build_model
    """

    def build_model(self, X, y, coords):
        """
        Defines the PyMC model:

        y ~ Normal(mu, sigma)
        sigma ~ HalfNormal(1)
        mu = X * beta
        beta ~ Dirichlet(1,...,1)

        Example
        --------

        """
        with self:
            self.add_coords(coords)
            n_predictors = X.shape[1]
            X = pm.MutableData("X", X, dims=["obs_ind", "coeffs"])
            y = pm.MutableData("y", y[:, 0], dims="obs_ind")
            # TODO: There we should allow user-specified priors here
            beta = pm.Dirichlet("beta", a=np.ones(n_predictors), dims="coeffs")
            # beta = pm.Dirichlet(
            #     name="beta", a=(1 / n_predictors) * np.ones(n_predictors),
            #     dims="coeffs"
            # )
            sigma = pm.HalfNormal("sigma", 1)
            mu = pm.Deterministic("mu", pm.math.dot(X, beta), dims="obs_ind")
            pm.Normal("y_hat", mu, sigma, observed=y, dims="obs_ind")


class LinearRegression(ModelBuilder):
    """
    Custom PyMC model for linear regression

    Public Methods
    ---------------
    * build_model
    """

    def build_model(self, X, y, coords):
        """
        Defines the PyMC model

        y ~ Normal(mu, sigma)
        mu = X * beta
        beta ~ Normal(0, 50)
        sigma ~ HalfNormal(1)

        Example
        --------

        """
        with self:
            self.add_coords(coords)
            X = pm.MutableData("X", X, dims=["obs_ind", "coeffs"])
            y = pm.MutableData("y", y[:, 0], dims="obs_ind")
            beta = pm.Normal("beta", 0, 50, dims="coeffs")
            sigma = pm.HalfNormal("sigma", 1)
            mu = pm.Deterministic("mu", pm.math.dot(X, beta), dims="obs_ind")
            pm.Normal("y_hat", mu, sigma, observed=y, dims="obs_ind")


class InstrumentalVariableRegression(ModelBuilder):
    """Custom PyMC model for instrumental linear regression"""

    def build_model(self, X, Z, y, t, coords, priors):
        """Specify model with treatment regression and focal regression data and priors

        :param X: A pandas dataframe used to predict our outcome y
        :param Z: A pandas dataframe used to predict our treatment variable t
        :param y: An array of values representing our focal outcome y
        :param t: An array of values representing the treatment t of
                  which we're interested in estimating the causal impact
        :param coords: A dictionary with the coordinate names for our
                       instruments and covariates
        :param priors: An optional dictionary of priors for the mus and
                      sigmas of both regressions

        :code:`priors = {"mus": [0, 0], "sigmas": [1, 1], "eta": 2, "lkj_sd": 2}`

        """

        # --- Priors ---
        with self:
            self.add_coords(coords)
            beta_t = pm.Normal(
                name="beta_t",
                mu=priors["mus"][0],
                sigma=priors["sigmas"][0],
                dims="instruments",
            )
            beta_z = pm.Normal(
                name="beta_z",
                mu=priors["mus"][1],
                sigma=priors["sigmas"][1],
                dims="covariates",
            )
            sd_dist = pm.HalfCauchy.dist(beta=priors["lkj_sd"], shape=2)
            chol, corr, sigmas = pm.LKJCholeskyCov(
                name="chol_cov",
                eta=priors["eta"],
                n=2,
                sd_dist=sd_dist,
            )
            # compute and store the covariance matrix
            pm.Deterministic(name="cov", var=pt.dot(l=chol, r=chol.T))

            # --- Parameterization ---
            mu_y = pm.Deterministic(name="mu_y", var=pm.math.dot(X, beta_z))
            # focal regression
            mu_t = pm.Deterministic(name="mu_t", var=pm.math.dot(Z, beta_t))
            # instrumental regression
            mu = pm.Deterministic(name="mu", var=pt.stack(tensors=(mu_y, mu_t), axis=1))

            # --- Likelihood ---
            pm.MvNormal(
                name="likelihood",
                mu=mu,
                chol=chol,
                observed=np.stack(arrays=(y.flatten(), t.flatten()), axis=1),
                shape=(X.shape[0], 2),
            )

    def fit(self, X, Z, y, t, coords, priors):
        """Draw samples from posterior, prior predictive, and posterior predictive
        distributions.
        """
        self.build_model(X, Z, y, t, coords, priors)
        with self.model:
            self.idata = pm.sample(**self.sample_kwargs)
            self.idata.extend(pm.sample_prior_predictive())
            self.idata.extend(
                pm.sample_posterior_predictive(self.idata, progressbar=False)
            )
        return self.idata
