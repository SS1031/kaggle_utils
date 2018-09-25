import itertools
import gc
import numpy as np
import pandas as pd
from abc import abstractmethod
from functools import partial
from multiprocessing.pool import Pool
from sklearn.base import TransformerMixin
from sklearn.decomposition import LatentDirichletAllocation, TruncatedSVD, NMF
from sklearn.feature_extraction.text import CountVectorizer, TfidfVectorizer, TfidfTransformer
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import OneHotEncoder
from .basic import BaseFeatureTransformer


def create_word_list(df, col1, col2):
    col1_size = df[col1].max() + 1
    col2_list = [[] for _ in range(col1_size)]
    for val1, val2 in zip(df[col1], df[col2]):
        col2_list[val1].append(val2)
    return [' '.join(map(str, list)) for list in col2_list]


class OneVsOneCoOccurrenceLatentVector(BaseFeatureTransformer):
    def create_document_term_matrix(self, dataframe, col1, col2):
        word_list = create_word_list(dataframe, col1, col2)
        vectorizer = self.vectorizer_factory()
        return vectorizer.fit_transform(word_list)

    def compute_latent_vectors(self, dataframe, col_pair):
        col1, col2 = col_pair
        document_term_matrix = self.create_document_term_matrix(dataframe, col1, col2)
        transformer = self.transformer_factory()
        return col1, col2, transformer.fit_transform(document_term_matrix)

    def transform(self, dataframe):
        column_pairs = self.get_column_pairs()

        col1s = []
        col2s = []
        latent_vectors = []

        with Pool(4) as p:
            for col1, col2, latent_vector in p.map(partial(self.compute_latent_vectors, dataframe), column_pairs):
                col1s.append(col1)
                col2s.append(col2)
                latent_vectors.append(latent_vector.astype(np.float32))
        gc.collect()
        return self.get_feature(train_path, col1s, col2s, latent_vectors), \
               self.get_feature(test_path, col1s, col2s, latent_vectors)

    def get_column_pairs(self):
        columns = ['ip', 'app', 'os', 'device', 'channel']
        return [(col1, col2) for col1, col2 in itertools.product(columns, repeat=2) if col1 != col2]

    @staticmethod
    def categorical_features():
        return []

    @property
    @abstractmethod
    def width(self):
        raise NotImplementedError

    @abstractmethod
    def transformer_factory(self):
        raise NotImplementedError

    @abstractmethod
    def vectorizer_factory(self):
        raise NotImplementedError

    def get_feature(self, path, cs1, cs2, vs):
        df_data = pd.read_feather(path)
        features = np.zeros(shape=(len(df_data), len(cs1) * self.width), dtype=np.float32)
        columns = []
        for i, (col1, col2, latent_vector) in enumerate(zip(cs1, cs2, vs)):
            offset = i * self.width
            for j in range(self.width):
                columns.append(self.name + '-' + col1 + '-' + col2 + '-' + str(j))
            for j, val1 in enumerate(df_data[col1]):
                features[j, offset:offset + self.width] = latent_vector[val1]

        return pd.DataFrame(data=features, columns=columns)


class KomakiLDA5(OneVsOneCoOccurrenceLatentVector):
    def vectorizer_factory(self):
        return CountVectorizer(min_df=2)

    def transformer_factory(self):
        return LatentDirichletAllocation(n_components=self.width, learning_method='online', random_state=71)

    @property
    def width(self):
        return 5


class KomakiPCA5(OneVsOneCoOccurrenceLatentVector):
    def vectorizer_factory(self):
        return TfidfVectorizer(min_df=2, dtype=np.float32)

    def transformer_factory(self) -> TransformerMixin:
        return TruncatedSVD(n_components=self.width, random_state=71)

    @property
    def width(self) -> int:
        return 5

class KomakiNMF5(OneVsOneCoOccurrenceLatentVector):
    def vectorizer_factory(self):
        return TfidfVectorizer(min_df=2, dtype=np.float32)

    def transformer_factory(self) -> TransformerMixin:
        return NMF(n_components=self.width, random_state=71)

    @property
    def width(self) -> int:
        return 5


class SinglePCACount(FeatherFeatureDF):
    @staticmethod
    def categorical_features():
        return []

    def create_features_from_dataframe(self, df_train: pd.DataFrame, df_test: pd.DataFrame):
        train_length = len(df_train)
        n_components = 30
        df_data: pd.DataFrame = pd.concat([df_train, df_test])
        pipeline = make_pipeline(
            OneHotEncoder(),
            TruncatedSVD(n_components=n_components, random_state=71)
        )
        features = pipeline.fit_transform(df_data[['ip', 'app', 'os', 'device', 'channel']].values).astype(np.float32)
        feature_columns = []
        for i in range(n_components):
            feature_columns.append(self.name + '_{}'.format(i))
        return pd.DataFrame(data=features[:train_length], columns=feature_columns), \
               pd.DataFrame(data=features[train_length:], columns=feature_columns)


class SinglePCATfIdf(FeatherFeatureDF):
    @staticmethod
    def categorical_features():
        return []

    def create_features_from_dataframe(self, df_train: pd.DataFrame, df_test: pd.DataFrame):
        train_length = len(df_train)
        n_components = 30
        df_data: pd.DataFrame = pd.concat([df_train, df_test])
        pipeline = make_pipeline(
            OneHotEncoder(),
            TfidfTransformer(),
            TruncatedSVD(n_components=30, random_state=71)
        )
        features = pipeline.fit_transform(df_data[['ip', 'app', 'os', 'device', 'channel']].values).astype(np.float32)
        feature_columns = []
        for i in range(n_components):
            feature_columns.append(self.name + '_{}'.format(i))
        return pd.DataFrame(data=features[:train_length], columns=feature_columns), \
               pd.DataFrame(data=features[train_length:], columns=feature_columns)


class UserItemLDA(FeatherFeatureDF):
    @staticmethod
    def categorical_features():
        return []

    def create_features_from_dataframe(self, df_train: pd.DataFrame, df_test: pd.DataFrame):
        train_length = len(df_train)
        n_components = 30
        threshold = 3
        df_data: pd.DataFrame = pd.concat([df_train, df_test])

        with simple_timer("Create document term matrix"):
            mask_to_id = {}
            values = []
            for ip, app, os, device, channel in zip(df_data.ip, df_data.app, df_data.os, df_data.device,
                                                    df_data.channel):
                mask = (ip << 44) | (device << 20) | (os << 10) | channel
                if mask in mask_to_id:
                    mask_id = mask_to_id[mask]
                else:
                    mask_id = len(mask_to_id)
                    mask_to_id[mask] = mask_id
                while len(values) <= mask_id:
                    values.append([])
                values[mask_id].append(app)
        with simple_timer("Create new mask id"):
            new_values = []
            new_mask_to_id = {}
            for mask, mask_id in mask_to_id.items():
                if len(values[mask_id]) >= threshold:
                    new_mask_id = len(new_values)
                    new_values.append(values[mask_id])
                    new_mask_to_id[mask] = new_mask_id
            values = new_values
            mask_to_id = new_mask_to_id
            del new_values, new_mask_to_id

        with simple_timer("Convert to documents"):
            values = [' '.join(map(str, ls)) for ls in values]
            print("Number of documents", len(values))

        with simple_timer("Vectorize document"):
            if len(df_data) < 1000 * 1000:
                vectorizer = CountVectorizer(min_df=1)
            else:
                vectorizer = CountVectorizer(min_df=2)
            values = vectorizer.fit_transform(values)

        with simple_timer("Run LDA"):
            lda = LatentDirichletAllocation(n_components=n_components, learning_method='online', random_state=71)
            components = lda.fit_transform(values)

        with simple_timer("Create feature matrix"):
            features = np.zeros(shape=(len(df_data), n_components), dtype=np.float32)
            for i, (ip, app, os, device, channel) in enumerate(
                    zip(df_data.ip, df_data.app, df_data.os, df_data.device, df_data.channel)):
                mask = (ip << 44) | (device << 20) | (os << 10) | channel
                if mask in mask_to_id:
                    mask_id = mask_to_id[mask]
                    features[i, :] = components[mask_id]

        feature_columns = [self.name + '_{}'.format(i) for i in range(n_components)]
        return pd.DataFrame(data=features[:train_length], columns=feature_columns), \
               pd.DataFrame(data=features[train_length:], columns=feature_columns)
