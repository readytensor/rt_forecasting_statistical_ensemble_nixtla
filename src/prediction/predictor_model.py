import os
import warnings
import joblib
import numpy as np
import pandas as pd
from schema.data_schema import ForecastingSchema
from sklearn.exceptions import NotFittedError
from gluonts.time_feature.seasonality import get_seasonality
from statsforecast import StatsForecast
from statsforecast.models import (
    AutoARIMA,
    AutoETS,
    AutoCES,
    DynamicOptimizedTheta,
)
from logger import get_logger

warnings.filterwarnings("ignore")


PREDICTOR_FILE_NAME = "predictor.joblib"
logger = get_logger(task_name="model_training")


class Forecaster:
    """A wrapper class for the Forecaster model.

    This class provides a consistent interface that can be used with other
    Forecaster models.
    """

    model_name = "Ensemble Forecaster"

    def __init__(
        self,
        data_schema: ForecastingSchema,
        season_length: int = None,
        history_forecast_ratio: int = None,
        random_state: int = 0,
        **kwargs,
    ):
        """Construct a new Ensemble Forecaster

        Args:

            data_schema (ForecastingSchema):Schema of training data.

            season_length (int): The season length of the time series.

            history_forecast_ratio (int):
                Sets the history length depending on the forecast horizon.
                For example, if the forecast horizon is 20 and the history_forecast_ratio is 10,
                history length will be 20*10 = 200 samples.

            random_state (int): Sets the underlying random seed at model initialization time.

            **kwargs (dict): Additional parameters accepted by the forecaster.
        """
        self.data_schema = data_schema
        self.random_state = random_state
        self._is_trained = False
        self.kwargs = kwargs
        self.history_length = None
        self.freq = self.map_frequency(self.data_schema.frequency)

        if season_length is not None:
            self.season_length = season_length
        elif self.freq != 1:
            self.season_length = get_seasonality(self.freq)
        else:
            self.season_length = 1

        if history_forecast_ratio:
            self.history_length = (
                self.data_schema.forecast_length * history_forecast_ratio
            )

        self.models = [
            AutoARIMA(season_length=self.season_length),
            AutoETS(season_length=self.season_length),
            AutoCES(season_length=self.season_length),
            DynamicOptimizedTheta(season_length=self.season_length),
        ]

        self.model = StatsForecast(
            models=self.models,
            freq=self.freq,
        )

    def map_frequency(self, frequency: str) -> str:
        """
        Maps the frequency in the data schema to the frequency expected by statsforecast.

        Args:
            frequency (str): The frequency from the schema.

        Returns (str): The mapped frequency.
        """
        if self.data_schema.time_col_dtype == "INT":
            return 1

        frequency = frequency.lower()
        frequency = frequency.split("frequency.")[1]
        if frequency == "yearly":
            return "Y"
        if frequency == "quarterly":
            return "Q"
        if frequency == "monthly":
            return "M"
        if frequency == "weekly":
            return "W"
        if frequency == "daily":
            return "D"
        if frequency == "hourly":
            return "H"
        if frequency == "minutely":
            return "min"
        if frequency == "secondly":
            return "S"
        else:
            return 1

    def prepare_data(self, data: pd.DataFrame) -> pd.DataFrame:
        """
        Prepares the training data by dropping past covariates, converting the index to datetime if available
        and drops or keeps other covariates depending on use_exogenous.

            Args:
                data (pd.DataFrame): The training data.
        """
        covariates = (
            self.data_schema.past_covariates
            + self.data_schema.future_covariates
            + self.data_schema.static_covariates
        )
        data.drop(columns=covariates, inplace=True)

        if self.data_schema.time_col_dtype in ["DATE", "DATETIME"]:
            data[self.data_schema.time_col] = pd.to_datetime(
                data[self.data_schema.time_col]
            )

        groups_by_ids = data.groupby(self.data_schema.id_col)
        all_ids = list(groups_by_ids.groups.keys())

        all_series = [groups_by_ids.get_group(id_).reset_index() for id_ in all_ids]

        if self.history_length:
            for index, series in enumerate(all_series):
                all_series[index] = series.iloc[-self.history_length :]
            data = pd.concat(all_series).drop(columns="index")

        data[self.data_schema.target] += 10
        return data

    def fit(
        self,
        history: pd.DataFrame,
    ) -> None:
        """Fit the Forecaster to the training data.

        Args:
            history (pandas.DataFrame): The features of the training data.

        """
        np.random.seed(self.random_state)

        history = self.prepare_data(history)

        self.model.fit(
            df=history,
            time_col=self.data_schema.time_col,
            id_col=self.data_schema.id_col,
            target_col=self.data_schema.target,
        )
        self._is_trained = True

    def predict(
        self, test_data: pd.DataFrame, prediction_col_name: str
    ) -> pd.DataFrame:
        """Make the forecast of given length.

        Args:
            test_data (pd.DataFrame): Given test input for forecasting.
            prediction_col_name (str): Name to give to prediction column.
        Returns:
            pd.DataFrame: The prediction dataframe.
        """
        if not self._is_trained:
            raise NotFittedError("Model is not fitted yet.")

        forecast = self.model.predict(self.data_schema.forecast_length)
        forecast.reset_index(inplace=True)
        forecast[prediction_col_name] = forecast.drop(
            columns=[self.data_schema.time_col, self.data_schema.id_col]
        ).mean(axis=1)

        forecast[self.data_schema.time_col] = test_data[self.data_schema.time_col]
        forecast[prediction_col_name] -= 10
        return forecast

    def save(self, model_dir_path: str) -> None:
        """Save the Forecaster to disk.

        Args:
            model_dir_path (str): Dir path to which to save the model.
        """
        if not self._is_trained:
            raise NotFittedError("Model is not fitted yet.")
        joblib.dump(self, os.path.join(model_dir_path, PREDICTOR_FILE_NAME))

    @classmethod
    def load(cls, model_dir_path: str) -> "Forecaster":
        """Load the Forecaster from disk.

        Args:
            model_dir_path (str): Dir path to the saved model.
        Returns:
            Forecaster: A new instance of the loaded Forecaster.
        """
        model = joblib.load(os.path.join(model_dir_path, PREDICTOR_FILE_NAME))
        return model

    def __str__(self):
        # sort params alphabetically for unit test to run successfully
        return f"Model name: {self.model_name}"


def train_predictor_model(
    history: pd.DataFrame,
    data_schema: ForecastingSchema,
    hyperparameters: dict,
) -> Forecaster:
    """
    Instantiate and train the predictor model.

    Args:
        history (pd.DataFrame): The training data inputs.
        data_schema (ForecastingSchema): Schema of the training data.
        hyperparameters (dict): Hyperparameters for the Forecaster.

    Returns:
        'Forecaster': The Forecaster model
    """

    model = Forecaster(
        data_schema=data_schema,
        **hyperparameters,
    )
    model.fit(
        history=history,
    )
    return model


def predict_with_model(
    model: Forecaster, test_data: pd.DataFrame, prediction_col_name: str
) -> pd.DataFrame:
    """
    Make forecast.

    Args:
        model (Forecaster): The Forecaster model.
        test_data (pd.DataFrame): The test input data for forecasting.
        prediction_col_name (int): Name to give to prediction column.

    Returns:
        pd.DataFrame: The forecast.
    """
    return model.predict(test_data, prediction_col_name)


def save_predictor_model(model: Forecaster, predictor_dir_path: str) -> None:
    """
    Save the Forecaster model to disk.

    Args:
        model (Forecaster): The Forecaster model to save.
        predictor_dir_path (str): Dir path to which to save the model.
    """
    if not os.path.exists(predictor_dir_path):
        os.makedirs(predictor_dir_path)
    model.save(predictor_dir_path)


def load_predictor_model(predictor_dir_path: str) -> Forecaster:
    """
    Load the Forecaster model from disk.

    Args:
        predictor_dir_path (str): Dir path where model is saved.

    Returns:
        Forecaster: A new instance of the loaded Forecaster model.
    """
    return Forecaster.load(predictor_dir_path)
