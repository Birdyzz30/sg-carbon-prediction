import params
import pandas as pd
import numpy as np

from pathlib import Path
from google.cloud import bigquery, storage

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
    def retrieve_data(self, table_id, order_by=None):
        try:
            query = f"""
                SELECT *
                FROM {params.PROJECT_ID}.{params.DATASET_ID}.{table_id}
                """
            if order_by:
                query += f" ORDER BY {order_by} ASC"

            client = bigquery.Client()
            query_job = client.query(query)
            result = query_job.result()
            df = result.to_dataframe()
            return df
        except Exception as e:
            raise Exception(f"An error occurred while retrieving '{table_id}' dataset: {str(e)}")

    def get_processed_from_bq(self):
        return self.retrieve_data(params.PROCESSED_TABLE_ID, order_by='planning_area')

    def get_predictions_from_bq(self):
        return self.retrieve_data(params.PREDICTIONS_TABLE_ID, order_by='planning_area')

class BigQueryDataLoader():
    @staticmethod
    def load_data(data: pd.DataFrame, table_id: str, truncate: bool) -> None:
        """
        Save the DataFrame to BigQuery.

        Parameters
        -------
        data : DataFrame
            The DataFrame to be saved.
        table_id : str
            The table ID to save data to.
        truncate : bool
            If True, empty the table before saving.

        """
        assert isinstance(data, pd.DataFrame)
        full_table_name = f"{params.PROJECT_ID}.{params.DATASET_ID}.{table_id}"
        data.columns = [f"_{column}" if not str(column)[0].isalpha() and not str(column)[0] == "_" else str(column) for column in data.columns]

        client = bigquery.Client()

        # Define write mode and schema
        write_mode = "WRITE_TRUNCATE" if truncate else "WRITE_APPEND"
        job_config = bigquery.LoadJobConfig(write_disposition=write_mode)

        # Load data
        job = client.load_table_from_dataframe(data, full_table_name, job_config=job_config)
        result = job.result()

        print(f"✅ Data saved to bigquery, with shape {data.shape}")

    @staticmethod
    def load_processed(data: pd.DataFrame, truncate: bool) -> None:
        BigQueryDataLoader.load_data(data, params.PROCESSED_TABLE_ID, truncate)

    def load_predictions(self, data: pd.DataFrame, truncate: bool) -> None:
        data = self.clean_pred_data(data)
        BigQueryDataLoader.load_data(data, params.PREDICTIONS_TABLE_ID, truncate)

    @staticmethod
    # Transform to required format for streamlit
    def clean_pred_data(data: pd.DataFrame) -> pd.DataFrame:
        #preparing list of area and years for 5 years
        frame_area = []
        frame_year = []
        for i in range (2022,2022+5):
            for j in params.PLANNING_AREA:
                frame_area.append(j)
                frame_year.append(i)
        #calculating total carbon (4t/human/year and 4.6t/vehicle/year)
        carbon_data = data
        carbon_data[2022] = carbon_data[0].apply(lambda x : x[0]+x[1]+x[2]*4+x[3]*4.6)
        carbon_data[2023] = carbon_data[1].apply(lambda x : x[0]+x[1]+x[2]*4+x[3]*4.6)
        carbon_data[2024] = carbon_data[2].apply(lambda x : x[0]+x[1]+x[2]*4+x[3]*4.6)
        carbon_data[2025] = carbon_data[3].apply(lambda x : x[0]+x[1]+x[2]*4+x[3]*4.6)
        carbon_data[2026] = carbon_data[4].apply(lambda x : x[0]+x[1]+x[2]*4+x[3]*4.6)

        #concat 2022-2026 total carbon vertically to merge into final dataframe
        combined_df = pd.concat([carbon_data.iloc[:,5],carbon_data.iloc[:,6],carbon_data.iloc[:,7],carbon_data.iloc[:,8],carbon_data.iloc[:,9]],axis=0)
        final_df = pd.DataFrame({'plan_area':frame_area,'year':frame_year,'carbon_total':combined_df}).reset_index(drop=True).sort_values(by=['plan_area','year'])

        return final_df


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

def split_train_test_data(df: pd.DataFrame):
    num_test_years = params.TEST_END - params.TEST_START + 1
    carbon_data = df.iloc[:, 1:-num_test_years].values
    target_data = df.iloc[:,-num_test_years:].values

    X = []  # Input features
    y = []  # Target values

    # Create input features and target values
    for i in range(0,len(carbon_data),len(params.TRANSFORMER_MAP)):
        X.append(carbon_data[i:i+len(params.TRANSFORMER_MAP)].T)
        y.append(target_data[i:i+len(params.TRANSFORMER_MAP)].T)

    X = np.array(X).astype(np.float32)
    y = np.array(y).astype(np.float32)

    split = int(len(X) *  0.8)
    X_train = X[:split]
    X_test = X[split:]
    y_train = y[:split]
    y_test = y[split:]

    return X_train, X_test, y_train, y_test
