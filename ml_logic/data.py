import params
import pandas as pd
import numpy as np
import time
import pickle
import glob

from pathlib import Path
from colorama import Fore, Style
from dateutil.parser import parse
from tensorflow import keras

import mlflow
from mlflow.tracking import MlflowClient

from google.cloud import bigquery, storage
from params import *

class BigQueryDataRetriever():
    """
    Retrieves dataset from BigQuery.

    Parameters
    -------
    table_id : str
        The table ID to retrieve the data from.

    Returns
    ------
    dataframe

    """
    def retrieve_data(self, table_id):
        try:
            query = f"""
                SELECT *
                FROM {params.PROJECT_ID}.{params.DATASET_ID}.{table_id}
                """

            client = bigquery.Client()
            query_job = client.query(query)
            result = query_job.result()
            df = result.to_dataframe()
            return df
        except Exception as e:
            raise Exception(f"An error occurred while retrieving '{table_id}' dataset: {str(e)}")


# Preprocess consumption data from EMA
class ConsumDataPreprocessingTransformer():
    """
    Retrieves EMA Electricity and Gas Consumption data from BigQuery and transform into the required format.

    Parameters
    -------
    table_id : str
        The table ID to retrieve the data from.

    Returns
    ------
    dataframe

    """
    def __init__(self, table_id):
        self.table_id = table_id
        self.data = BigQueryDataRetriever().retrieve_data(self.table_id)

    def clean_data(self):
        X = self.data

        try:
            X = self.filter_data(X)
            X = self.rename_columns(X)
            X = self.convert_to_numeric(X)
            X = self.merge_with_household_data(X)
            X = self.calculate_columns(X)

            return X
        except ValueError as ve:
            raise ValueError(f"An error occurred while cleaning '{self.table_id}' dataset: {str(ve)}")

    def filter_data(self, X):
        X = X[(X['month'] == 'Annual') & (X['year'] >= params.YEAR_START) & (X['year'] <= params.YEAR_END)]
        X = X.drop(columns=['month', 'Region'])
        X = X[~(X['dwelling_type'].str.lower().str.contains('housing') | (X['dwelling_type'] == 'Overall'))]
        X['year'] = X['year'].astype(int)
        return X

    def rename_columns(self, X):
        X = X.rename(columns={'Description': 'planning_area'})
        return X

    def convert_to_numeric(self, X):
        X['kwh_per_acc'] = pd.to_numeric(X['kwh_per_acc'], errors='coerce')
        X = X[(X['kwh_per_acc'].notna()) & (X['kwh_per_acc'] > 0)]
        return X

    def merge_with_household_data(self, X):
        Household_data = HouseholdDataPreprocessingTransformer().clean_data()
        X = X.merge(Household_data, on=['dwelling_type', 'year', 'planning_area'], how='left')
        return X

    def calculate_columns(self, X):
        X['kwh_total'] = X['kwh_per_acc'] * X['household_acc']
        X['co2_emissions_ton_total'] = X['kwh_total'] * params.ELEC_EMISSION_FACTOR * 0.001
        X = X.drop(columns=['kwh_per_acc', 'household_acc', 'kwh_total'])
        X = X.groupby(['planning_area', 'year']).agg({'co2_emissions_ton_total': 'sum'}).reset_index()
        X = X.pivot(index=['planning_area'], columns='year', values='co2_emissions_ton_total').reset_index()
        X = X[~(X['planning_area'].str.lower().str.contains('region') | (X['planning_area'] == 'Overall'))]
        return X


class HouseholdDataPreprocessingTransformer():
    """
    Retrieves Datagov household data from BigQuery and transform into the required format.

    Parameters
    -------
    table_id : str
        The table ID to retrieve the data from.

    Returns
    ------
    dataframe

    """
    def __init__(self):
        self.table_id = 'household'
        self.data = BigQueryDataRetriever().retrieve_data(self.table_id)

    def clean_data(self):
        X = self.data

        try:
            X = X.rename(columns={'PA': 'planning_area', 'TOD': 'dwelling_type', 'Hse': 'household_acc', 'Time': 'year'})
            X = X[(X['year'] >= params.YEAR_START) & (X['year'] <= params.YEAR_END)]
            X = X.groupby(['planning_area', 'dwelling_type', 'year']).agg({'household_acc': 'sum'}).reset_index()
            X['dwelling_type'] = X['dwelling_type'].map(params.DWELLING_TYPE_MAPPING)
            return X
        except Exception as e:
            print(f"An error occurred while cleaning '{self.table_id}' dataset: {str(e)}")
            return None


class ElecConsumDataPreprocessingTransformer(ConsumDataPreprocessingTransformer):
    def __init__(self):
        super().__init__('electricity_consumption')


class GasConsumDataPreprocessingTransformer(ConsumDataPreprocessingTransformer):
    def __init__(self):
        super().__init__('gas_consumption')


class PopulationDataPreprocessingTransformer():
    """
    Retrieves Datagov population data from BigQuery and transform into the required format.

    Parameters
    -------
    table_id : str
        The table ID to retrieve the data from.

    Returns
    ------
    dataframe

    """
    def __init__(self):
        self.table_id = 'population'
        self.data = BigQueryDataRetriever().retrieve_data(self.table_id)

    def clean_data(self):
        X = self.data

        try:
            X = X.rename(columns={'PA': 'planning_area'})
            X = X[(X['Time'] >= params.YEAR_START) & (X['Time'] <= params.YEAR_END)]
            X = X.pivot(index='planning_area', columns='Time', values='Pop').reset_index()
            return X
        except Exception as e:
            print(f"An error occurred while cleaning '{self.table_id}' dataset: {str(e)}")
            return None


class VehicleDataPreprocessingTransformer():
    """
    Retrieves OneMap vehicle data from BigQuery and transform into the required format.

    Parameters
    -------
    table_id : str
        The table ID to retrieve the data from.

    Returns
    ------
    dataframe

    """
    def __init__(self):
        self.table_id = 'vehicle'
        self.data = BigQueryDataRetriever().retrieve_data(self.table_id)

    def clean_data(self):
        X = self.data
        try:
            X = X[(X['year'] >= params.YEAR_START) & (X['year'] <= params.YEAR_END)]
            X = X.pivot(index='planning_area', columns='year', values='vehicle').reset_index()
            return X
        except Exception as e:
            print(f"An error occurred while cleaning '{self.table_id}' dataset: {str(e)}")
            return None


def combine_clean_data():
    """
    Creates a combined Dataframe for all the available data categories and adds empty rows for any missing planning area.

    Returns
    ------
    dataframe

    """
    dfs = pd.DataFrame()

    # Create combined dataset
    for data_category in params.TRANSFORMER_MAP.keys():
        dfs = add_dataset_to_list(data_category, dfs)

    # Add empty row for missing planning_area
    dfs = add_missing_planning_area(dfs)

    # Clean combined data and process to X and y
    dfs = clean_combined_data(dfs)

    return dfs

def add_dataset_to_list(data_category, dfs):
    """
    Appends a DataFrame of a specific data category to an existing DataFrame and adds the data category as a suffix to each planning area.

    Parameters
    -------
    data_category : str
        Data category to be added.
    dfs : dataframe
        DataFrame to which the data will be appended.

    Returns
    ------
    dataframe

    """
    transformer_class = globals()[params.TRANSFORMER_MAP.get(data_category)]
    transformer = transformer_class()

    df = transformer.clean_data()
    df['planning_area'] = df['planning_area'] + f'_{data_category}'
    return pd.concat([dfs, df])

def get_transformer(data_category):
    return params.TRANSFORMER_MAP.get(data_category)

def add_missing_planning_area(df):
    """
    Adds missing planning areas to an existing combined DataFrame.

    Parameters
    -------
    dfs : dataframe
        Existing combined DataFrame for all data categories with all the unique planning area.

    Returns
    ------
    dataframe

    """
    all_df = [{'planning_area': f'{planning_area}_{data_category}'}
                for planning_area in params.PLANNING_AREA
                for data_category in params.TRANSFORMER_MAP.keys()]

    all_df = pd.DataFrame(all_df)

    merged_df = pd.merge(all_df, df, on='planning_area', how='left')

    return merged_df

def clean_combined_data(df):
    df[df.columns[1:]] = df[df.columns[1:]].fillna(0).astype(int)
    df = df.sort_values(by='planning_area')
    return df

def load_data_to_bq(
        data: pd.DataFrame,
        gcp_project:str,
        bq_dataset:str,
        table: str,
        truncate: bool
    ) -> None:
    """
    - Save the DataFrame to BigQuery
    - Empty the table beforehand if `truncate` is True, append otherwise
    """

    assert isinstance(data, pd.DataFrame)
    full_table_name = f"{gcp_project}.{bq_dataset}.{table}"
    print(Fore.BLUE + f"\nSave data to BigQuery @ {full_table_name}...:" + Style.RESET_ALL)

    # Load data onto full_table_name

    # 🎯 HINT for "*** TypeError: expected bytes, int found":
    # After preprocessing the data, your original column names are gone (print it to check),
    # so ensure that your column names are *strings* that start with either
    # a *letter* or an *underscore*, as BQ does not accept anything else

    # TODO: simplify this solution if possible, but students may very well choose another way to do it
    # We don't test directly against their own BQ tables, but only the result of their query
    data.columns = [f"_{column}" if not str(column)[0].isalpha() and not str(column)[0] == "_" else str(column) for column in data.columns]

    client = bigquery.Client()

    # Define write mode and schema
    write_mode = "WRITE_TRUNCATE" if truncate else "WRITE_APPEND"
    job_config = bigquery.LoadJobConfig(write_disposition=write_mode)

    print(f"\n{'Write' if truncate else 'Append'} {full_table_name} ({data.shape[0]} rows)")

    # Load data
    job = client.load_table_from_dataframe(data, full_table_name, job_config=job_config)
    result = job.result()  # wait for the job to complete

    print(f"✅ Data saved to bigquery, with shape {data.shape}")

def get_data_with_cache(
        gcp_project:str,
        query:str,
        cache_path:Path,
        data_has_header=True
    ) -> pd.DataFrame:
    """
    Retrieve `query` data from BigQuery, or from `cache_path` if the file exists
    Store at `cache_path` if retrieved from BigQuery for future use
    """
    if cache_path.is_file():
        print(Fore.BLUE + "\nLoad data from local CSV..." + Style.RESET_ALL)
        df = pd.read_csv(cache_path, header='infer' if data_has_header else None)
    else:
        print(Fore.BLUE + "\nLoad data from BigQuery server..." + Style.RESET_ALL)
        client = bigquery.Client(project=gcp_project)
        query_job = client.query(query)
        result = query_job.result()
        df = result.to_dataframe()

        # Store as CSV if the BQ query returned at least one valid line
        if df.shape[0] > 1:
            df.to_csv(cache_path, header=data_has_header, index=False)

    print(f"✅ Data loaded, with shape {df.shape}")

    return df


def save_model(model) -> None:
    """
    Persist trained model locally on the hard drive at f"{LOCAL_REGISTRY_PATH}/models/{timestamp}.h5"
    - if MODEL_TARGET='gcs', also persist it in your bucket on GCS at "models/{timestamp}.h5" --> unit 02 only
    - if MODEL_TARGET='mlflow', also persist it on MLflow instead of GCS (for unit 0703 only) --> unit 03 only
    """

    timestamp = time.strftime("%Y%m%d-%H%M%S")

    # Save model locally
    folder_path = os.path.join(LOCAL_REGISTRY_PATH, "models")
    if os.path.isdir(folder_path):
        model_path = os.path.join(folder_path, f"{timestamp}.h5")
        model.save(model_path)
    else:
        os.mkdir(folder_path)
        model_path = os.path.join(folder_path, f"{timestamp}.h5")
        model.save(model_path)

    print("✅ Model saved to local machine")

    if MODEL_TARGET == "gcs":
        # 🎁 We give you this piece of code as a gift. Please read it carefully! Add a breakpoint if needed!

        model_filename = model_path.split("/")[-1] # e.g. "20230208-161047.h5" for instance
        client = storage.Client()
        bucket = client.bucket(BUCKET_NAME)
        blob = bucket.blob(f"models/{model_filename}")
        blob.upload_from_filename(model_path)

        print("✅ Model saved to GCS")

        return None

    # mlflow.tensorflow.log_model(
    #         model=model,
    #         artifact_path="model",
    #         registered_model_name=MLFLOW_MODEL_NAME
    #     )

    # print("✅ Model saved to MLflow")

    return None

def save_results(params: dict, metrics: dict) -> None:
    """
    Persist params & metrics locally on the hard drive at
    "{LOCAL_REGISTRY_PATH}/params/{current_timestamp}.pickle"
    "{LOCAL_REGISTRY_PATH}/metrics/{current_timestamp}.pickle"
    - (unit 03 only) if MODEL_TARGET='mlflow', also persist them on MLflow
    """

    timestamp = time.strftime("%Y%m%d-%H%M%S")

    # Save params locally
    if params is not None:
        folder_path = os.path.join(LOCAL_REGISTRY_PATH, "params")
        if os.path.isdir(folder_path):
            params_path = os.path.join(folder_path, timestamp + ".pickle")
            with open(params_path, "wb") as file:
                pickle.dump(params, file)
        else:
            os.mkdir(folder_path)
            params_path = os.path.join(folder_path, timestamp + ".pickle")
            with open(params_path, "wb") as file:
                pickle.dump(params, file)

    # Save metrics locally
    if metrics is not None:
        folder_path = os.path.join(LOCAL_REGISTRY_PATH, "metrics")
        if os.path.isdir(folder_path):
            metrics_path = os.path.join(folder_path, timestamp + ".pickle")
            with open(metrics_path, "wb") as file:
                pickle.dump(metrics, file)
        else:
            os.mkdir(folder_path)
            metrics_path = os.path.join(folder_path, timestamp + ".pickle")
            with open(metrics_path,"wb") as file:
                pickle.dump(metrics, file)

    print("✅ Results saved locally")
    # if params is not None:
    #     mlflow.log_params(params)
    # if metrics is not None:
    #     mlflow.log_metrics(metrics)
    # print("✅ Results saved on MLflow")


def load_model(stage = "production"):

    if MODEL_TARGET == "local":

        print(Fore.BLUE + f"\nLoad latest model from local registry..." + Style.RESET_ALL)

        # Get the latest model version name by the timestamp on disk
        local_model_directory = os.path.join(LOCAL_REGISTRY_PATH, "models")
        local_model_paths = glob.glob(f"{local_model_directory}/*")

        if not local_model_paths:
            return None

        most_recent_model_path_on_disk = sorted(local_model_paths)[-1]

        print(Fore.BLUE + f"\nLoad latest model from disk..." + Style.RESET_ALL)

        latest_model = keras.models.load_model(most_recent_model_path_on_disk)

        print("✅ Model loaded from local disk")

        return latest_model

    elif MODEL_TARGET == "gcs":
        # 🎁 We give you this piece of code as a gift. Please read it carefully! Add a breakpoint if needed!
        print(Fore.BLUE + f"\nLoad latest model from GCS..." + Style.RESET_ALL)

        client = storage.Client()
        blobs = list(client.get_bucket(BUCKET_NAME).list_blobs(prefix="model"))

        try:
            latest_blob = max(blobs, key=lambda x: x.updated)
            latest_model_path_to_save = os.path.join(LOCAL_REGISTRY_PATH, latest_blob.name)
            latest_blob.download_to_filename(latest_model_path_to_save)

            latest_model = keras.models.load_model(latest_model_path_to_save)

            print("✅ Latest model downloaded from cloud storage")

            return latest_model
        except:
            print(f"\n❌ No model found in GCS bucket {BUCKET_NAME}")

            return None


    # print(Fore.BLUE + f"\nLoad [{stage}] model from MLflow..." + Style.RESET_ALL)

    # # Load model from MLflow
    # model = None
    # mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
    # client = MlflowClient()

    # try:
    #     model_versions = client.get_latest_versions(name=MLFLOW_MODEL_NAME, stages=[stage])
    #     model_uri = model_versions[0].source

    #     assert model_uri is not None
    # except:
    #     print(f"\n❌ No model found with name {MLFLOW_MODEL_NAME} in stage {stage}")

    #     return None

    # model = mlflow.tensorflow.load_model(model_uri=model_uri)

    # print("✅ Model loaded from MLflow")

    # return model

def mlflow_transition_model(current_stage: str, new_stage: str) -> None:
    """
    Transition the latest model from the `current_stage` to the
    `new_stage` and archive the existing model in `new_stage`
    """
    mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)

    client = MlflowClient()

    version = client.get_latest_versions(name=MLFLOW_MODEL_NAME, stages=[current_stage])

    if not version:
        print(f"\n❌ No model found with name {MLFLOW_MODEL_NAME} in stage {current_stage}")
        return None

    client.transition_model_version_stage(
        name=MLFLOW_MODEL_NAME,
        version=version[0].version,
        stage=new_stage,
        archive_existing_versions=True
    )

    print(f"✅ Model {MLFLOW_MODEL_NAME} (version {version[0].version}) transitioned from {current_stage} to {new_stage}")

    return None
