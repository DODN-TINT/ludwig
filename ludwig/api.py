# !/usr/bin/env python
# coding=utf-8
# Copyright (c) 2019 Uber Technologies, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""
    File name: LudwigModel.py
    Author: Piero Molino
    Date created: 5/21/2019
    Date last modified: 5/21/2019
    Python Version: 3+
"""
import copy
import logging
import os
import tempfile
from pprint import pformat
from typing import List, Union, Dict, Tuple, Optional

import numpy as np
import pandas as pd

import ludwig.contrib

ludwig.contrib.contrib_import()

from ludwig.constants import PREPROCESSING, TRAINING, VALIDATION, TEST, FULL
from ludwig.contrib import contrib_command
from ludwig.data.postprocessing import convert_predictions, postprocess
from ludwig.data.preprocessing import preprocess_for_training, \
    preprocess_for_prediction, load_metadata
from ludwig.features.feature_registries import \
    update_model_definition_with_metadata
from ludwig.globals import TRAIN_SET_METADATA_FILE_NAME, \
    MODEL_HYPERPARAMETERS_FILE_NAME, \
    MODEL_WEIGHTS_FILE_NAME, set_disable_progressbar
from ludwig.models.ecd import ECD
from ludwig.models.predictor import Predictor, save_prediction_outputs, \
    calculate_overall_stats, print_evaluation_stats, save_evaluation_stats
from ludwig.models.trainer import Trainer
from ludwig.modules.metric_modules import get_best_function
from ludwig.utils.data_utils import save_json, load_json, \
    generate_kfold_splits, figure_data_format, DATAFRAME_FORMATS, \
    DICT_FORMATS, CACHEABLE_FORMATS, external_data_reader_registry
from ludwig.utils.horovod_utils import broadcast_return, configure_horovod, \
    set_on_master, \
    is_on_master
from ludwig.utils.misc_utils import get_output_directory, get_file_names, \
    get_experiment_description, get_from_registry
from ludwig.utils.tf_utils import initialize_tensorflow

import yaml

from ludwig.utils.defaults import default_random_seed, merge_with_defaults
from ludwig.utils.print_utils import print_boxed

logger = logging.getLogger(__name__)


class LudwigModel:
    def __init__(self,
                 model_definition,
                 logging_level=logging.ERROR,
                 use_horovod=None,
                 gpus=None,
                 gpu_memory_limit=None,
                 allow_parallel_threads=True):
        """
        :param model_definition: (dict, str) in-memory representation of model definition
               or string path to the saved JSON model definition file.
        :param logging_level: Log level that will be sent to stderr.
        :param use_horovod: (bool) use Horovod for distributed training. Will be set
               automatically if `horovodrun` is used to launch the training script.
        :param gpus: (str, default: `None`) list of GPUs to use (it uses the
               same syntax of CUDA_VISIBLE_DEVICES)
        :param gpu_memory_limit: (int: default: `None`) maximum memory in MB to allocate
              per GPU device.
        :param allow_parallel_threads: (bool, default: `True`) allow TensorFlow to use
               multithreading parallelism to improve performance at the cost of
               determinism.
        """
        # check if model definition is a path or a dict
        if isinstance(model_definition, str):  # assume path
            with open(model_definition, 'r') as def_file:
                model_definition_dict = yaml.safe_load(def_file)
            self.model_definition_fp = model_definition
        else:
            model_definition_dict = copy.deepcopy(model_definition)
            self.model_definition_fp = None

        # merge model definition with defaults
        self.model_definition = merge_with_defaults(model_definition_dict)

        # setup horovod
        self._horovod = configure_horovod(use_horovod)

        # setup logging
        self.set_logging_level(logging_level)

        # setup TensorFlow
        initialize_tensorflow(gpus, gpu_memory_limit, allow_parallel_threads,
                              self._horovod)

        # setup model
        self.model = None
        self.training_set_metadata = None

        # online training state
        self._online_trainer = None

    def train(
            self,
            dataset: Union[str, dict, pd.DataFrame] = None,
            training_set: Union[str, dict, pd.DataFrame] = None,
            validation_set: Union[str, dict, pd.DataFrame] = None,
            test_set: Union[str, dict, pd.DataFrame] = None,
            training_set_metadata=None,
            data_format=None,
            experiment_name='api_experiment',
            model_name='run',
            model_resume_path=None,
            skip_save_training_description=False,
            skip_save_training_statistics=False,
            skip_save_model=False,
            skip_save_progress=False,
            skip_save_log=False,
            skip_save_processed_input=False,
            output_directory='results',
            random_seed=default_random_seed,
            debug=False,
            **kwargs
    ) -> Tuple[dict, Union[dict, pd.DataFrame], str]:
        """This function is used to perform a full training of the model on the
           specified dataset.

        # Inputs

        :param dataset: (Union[str, dict, pandas.DataFrame], default: `None`)
            source containing the entire dataset to be used in the experiment.
            If it has a split column, it will be used for splitting (0 for train,
            1 for validation, 2 for test), otherwise the dataset will be
            randomly split.
        :param training_set: (Union[str, dict, pandas.DataFrame], default: `None`)
            source containing training data.
        :param validation_set: (Union[str, dict, pandas.DataFrame], default: `None`)
            source containing validation data.
        :param test_set: (Union[str, dict, pandas.DataFrame], default: `None`)
            source containing test data.
        :param training_set_metadata: (Union[str, dict], default: `None`)
            metadata JSON file or loaded metadata.  Intermediate preprocess
            structure containing the mappings of the input
            dataset created the first time an input file is used in the same
            directory with the same name and a '.meta.json' extension.
        :param data_format: (str, default: `None`) format to interpret data
            sources. Will be inferred automatically if not specified.  Valid
            formats are `'auto'`, `'csv'`, `'df'`, `'dict'`, `'excel'`, `'feather'`,
            `'fwf'`, `'hdf5'` (cache file produced during previous training),
            `'html'` (file containing a single HTML `<table>`), `'json'`, `'jsonl'`,
            `'parquet'`, `'pickle'` (pickled Pandas DataFrame), `'sas'`, `'spss'`,
            `'stata'`, `'tsv'`.
        :param dataset: (Union[str, dict, pandas.DataFrame], default: `None`)
            source containing the entire dataset to be used in the experiment.
            If it has a split column, it will be used for splitting (0 for train,
            1 for validation, 2 for test), otherwise the dataset will be
            randomly split.
        :param training_set: (Union[str, dict, pandas.DataFrame], default: `None`)
            source containing training data.
        :param validation_set: (Union[str, dict, pandas.DataFrame], default: `None`)
            source containing validation data.
        :param test_set: (Union[str, dict, pandas.DataFrame], default: `None`)
            source containing test data.
        :param training_set_metadata: (Union[str, dict], default: `None`)
            metadata JSON file or loaded metadata.  Intermediate preprocess
            structure containing the mappings of the input
            dataset created the first time an input file is used in the same
            directory with the same name and a '.meta.json' extension.
        :param data_format: (str, default: `None`) format to interpret data
            sources. Will be inferred automatically if not specified.  Valid
            formats are `'auto'`, `'csv'`, `'df'`, `'dict'`, `'excel'`, `'feather'`,
            `'fwf'`, `'hdf5'` (cache file produced during previous training),
            `'html'` (file containing a single HTML `<table>`), `'json'`, `'jsonl'`,
            `'parquet'`, `'pickle'` (pickled Pandas DataFrame), `'sas'`, `'spss'`,
            `'stata'`, `'tsv'`.
        :param experiment_name: (str, default: `'experiment'`) name for
            the experiment.
        :param model_name: (str, default: `'run'`) name of the model that is
            being used.
        :param model_resume_path: (str, default: `None`) resumes training of
            the model from the path specified. The model definition is restored.
            In addition to model definition, training statistics, loss for each
            epoch and the state of the optimizer are restored such that
            training can be effectively continued from a previously interrupted
            training process.
        :param skip_save_training_description: (bool, default: `False`) disables
            saving the description JSON file.
        :param skip_save_training_statistics: (bool, default: `False`) disables
            saving training statistics JSON file.
        :param skip_save_model: (bool, default: `False`) disables
            saving model weights and hyperparameters each time the model
            improves. By default Ludwig saves model weights after each epoch
            the validation metric improves, but if the model is really big
            that can be time consuming if you do not want to keep
            the weights and just find out what performance can a model get
            with a set of hyperparameters, use this parameter to skip it,
            but the model will not be loadable later on and the returned model
            will have the weights obtained at the end of training, instead of
            the weights of the epoch with the best validation performance.
        :param skip_save_progress: (bool, default: `False`) disables saving
            progress each epoch. By default Ludwig saves weights and stats
            after each epoch for enabling resuming of training, but if
            the model is really big that can be time consuming and will uses
            twice as much space, use this parameter to skip it, but training
            cannot be resumed later on.
        :param skip_save_log: (bool, default: `False`) disables saving
            TensorBoard logs. By default Ludwig saves logs for the TensorBoard,
            but if it is not needed turning it off can slightly increase the
            overall speed.
        :param skip_save_processed_input: (bool, default: `False`) if input
            dataset is provided it is preprocessed and cached by saving an HDF5
            and JSON files to avoid running the preprocessing again. If this
            parameter is `False`, the HDF5 and JSON file are not saved.
        :param output_directory: (str, default: `'results'`) the directory that
            will contain the training statistics, TensorBoard logs, the saved
            model and the training progress files.
        :param random_seed: (int, default: `42`) a random seed that is going to be
               used anywhere there is a call to a random number generator: data
               splitting, parameter initialization and training set shuffling
        :param debug: (bool, default: `False`)  if `True` turns on `tfdbg` with
            `inf_or_nan` checks.

        During training the model and statistics will be saved in a directory
        `[output_dir]/[experiment_name]_[model_name]_n` where all variables are
        resolved to user spiecified ones and `n` is an increasing number
        starting from 0 used to differentiate different runs.


        # Return

        :return: (Tuple[dict, Union[dict, pd.DataFrame], str]) tuple containing
            `(training_statistics, preprocessed_data, output_directory)`.
            `training_statistics` is a dictionary of training statistics for each
            output feature containing loss and metrics values for each epoch.
            `preprocessed_data` is the tuple containing these three data sets
            `(training_set, validation_set, test_set)`.  `output_directory`
            filepath to where training results are stored.
        """
        # setup directories and file names
        if model_resume_path is not None:
            if os.path.exists(model_resume_path):
                output_directory = model_resume_path
            else:
                if is_on_master():
                    logger.info(
                        'Model resume path does not exists, '
                        'starting training from scratch'
                    )
                model_resume_path = None

        if model_resume_path is None:
            if is_on_master():
                output_directory = get_output_directory(
                    output_directory,
                    experiment_name,
                    model_name
                )
            else:
                output_directory = None

        # if we are skipping all saving,
        # there is no need to create a directory that will remain empty
        should_create_output_directory = not (
                skip_save_training_description and
                skip_save_training_statistics and
                skip_save_model and
                skip_save_progress and
                skip_save_log and
                skip_save_processed_input
        )

        description_fn = training_stats_fn = model_dir = None
        if is_on_master():
            if should_create_output_directory:
                if not os.path.exists(output_directory):
                    os.makedirs(output_directory, exist_ok=True)
            description_fn, training_stats_fn, model_dir = get_file_names(
                output_directory)

        # save description
        if is_on_master():
            description = get_experiment_description(
                self.model_definition,
                dataset=dataset,
                training_set=training_set,
                validation_set=validation_set,
                test_set=test_set,
                training_set_metadata=training_set_metadata,
                data_format=data_format,
                random_seed=random_seed
            )
            if not skip_save_training_description:
                save_json(description_fn, description)
            # print description
            logger.info('Experiment name: {}'.format(experiment_name))
            logger.info('Model name: {}'.format(model_name))
            logger.info('Output directory: {}'.format(output_directory))
            logger.info('\n')
            for key, value in description.items():
                logger.info('{}: {}'.format(key, pformat(value, indent=4)))
            logger.info('\n')

        # preprocess
        preprocessed_data = preprocess_for_training(
            self.model_definition,
            dataset=dataset,
            training_set=training_set,
            validation_set=validation_set,
            test_set=test_set,
            training_set_metadata=training_set_metadata,
            data_format=data_format,
            skip_save_processed_input=skip_save_processed_input,
            preprocessing_params=self.model_definition[PREPROCESSING],
            random_seed=random_seed
        )

        (training_set,
         validation_set,
         test_set,
         training_set_metadata) = preprocessed_data
        self.training_set_metadata = training_set_metadata

        if is_on_master():
            logger.info('Training set: {0}'.format(training_set.size))
            if validation_set is not None:
                logger.info('Validation set: {0}'.format(validation_set.size))
            if test_set is not None:
                logger.info('Test set: {0}'.format(test_set.size))

        if is_on_master():
            if not skip_save_model:
                # save train set metadata
                os.makedirs(model_dir, exist_ok=True)
                save_json(
                    os.path.join(
                        model_dir,
                        TRAIN_SET_METADATA_FILE_NAME
                    ),
                    training_set_metadata
                )

        contrib_command("train_init", experiment_directory=output_directory,
                        experiment_name=experiment_name, model_name=model_name,
                        output_directory=output_directory,
                        resume=model_resume_path is not None)

        # Build model if not provided
        # if it was provided it means it was already loaded
        if not self.model:
            if is_on_master():
                print_boxed('MODEL', print_fun=logger.debug)
            # update model definition with metadata properties
            update_model_definition_with_metadata(
                self.model_definition,
                training_set_metadata
            )
            self.model = LudwigModel.create_model(self.model_definition,
                                                  random_seed=random_seed)

        # init trainer
        trainer = Trainer(
            **self.model_definition[TRAINING],
            resume=model_resume_path is not None,
            skip_save_model=skip_save_model,
            skip_save_progress=skip_save_progress,
            skip_save_log=skip_save_log,
            random_seed=random_seed,
            horoovd=self._horovod,
            debug=debug
        )

        contrib_command("train_model", self.model, self.model_definition,
                        self.model_definition_fp)

        # train model
        if is_on_master():
            print_boxed('TRAINING')
            if not skip_save_model:
                self.save_model_definition(model_dir)

        train_stats = trainer.train(
            self.model,
            training_set,
            validation_set=validation_set,
            test_set=test_set,
            save_path=model_dir,
        )

        train_trainset_stats, train_valiset_stats, train_testset_stats = train_stats
        train_stats = {
            TRAINING: train_trainset_stats,
            VALIDATION: train_valiset_stats,
            TEST: train_testset_stats
        }

        # save training statistics
        if is_on_master():
            if not skip_save_training_statistics:
                save_json(training_stats_fn, train_stats)

        # grab the results of the model with highest validation test performance
        validation_field = trainer.validation_field
        validation_metric = trainer.validation_metric
        validation_field_result = train_valiset_stats[validation_field]

        best_function = get_best_function(validation_metric)
        # results of the model with highest validation test performance
        if is_on_master() and validation_set is not None:
            epoch_best_vali_metric, best_vali_metric = best_function(
                enumerate(validation_field_result[validation_metric]),
                key=lambda pair: pair[1]
            )
            logger.info(
                'Best validation model epoch: {0}'.format(
                    epoch_best_vali_metric + 1)
            )
            logger.info(
                'Best validation model {0} on validation set {1}: {2}'.format(
                    validation_metric, validation_field, best_vali_metric
                ))
            if test_set is not None:
                best_vali_metric_epoch_test_metric = train_testset_stats[
                    validation_field][validation_metric][
                    epoch_best_vali_metric]

                logger.info(
                    'Best validation model {0} on test set {1}: {2}'.format(
                        validation_metric,
                        validation_field,
                        best_vali_metric_epoch_test_metric
                    )
                )
            logger.info(
                '\nFinished: {0}_{1}'.format(experiment_name, model_name))
            logger.info('Saved to: {0}'.format(output_directory))

        contrib_command("train_save", output_directory)

        self.training_set_metadata = training_set_metadata

        if not skip_save_model:
            # Load the best weights from saved checkpoint
            self.load_weights(model_dir)

        return train_stats, preprocessed_data, output_directory

    def train_online(
            self,
            dataset: Union[str, dict, pd.DataFrame],
            training_set_metadata: Union[str, dict] = None,
            data_format: str = 'auto',
            random_seed: int = default_random_seed,
            debug: bool = False
    ) -> None:
        """Performs one epoch of training of the model on `dataset`.

        # Inputs

        :param dataset: (Union[str, dict, pandas.DataFrame], default: `None`)
            source containing the entire dataset to be used in the experiment.
            If it has a split column, it will be used for splitting (0 for train,
            1 for validation, 2 for test), otherwise the dataset will be
            randomly split.
        :param training_set_metadata: (Union[str, dict], default: `None`)
            metadata JSON file or loaded metadata.  Intermediate preprocess
            structure containing the mappings of the input
            dataset created the first time an input file is used in the same
            directory with the same name and a '.meta.json' extension.
        :param data_format: (str, default: `None`) format to interpret data
            sources. Will be inferred automatically if not specified.  Valid
            formats are `'auto'`, `'csv'`, `'df'`, `'dict'`, `'excel'`, `'feather'`,
            `'fwf'`, `'hdf5'` (cache file produced during previous training),
            `'html'` (file containing a single HTML `<table>`), `'json'`, `'jsonl'`,
            `'parquet'`, `'pickle'` (pickled Pandas DataFrame), `'sas'`, `'spss'`,
            `'stata'`, `'tsv'`.
        :param random_seed: (int, default: `42`) a random seed that is going to be
               used anywhere there is a call to a random number generator: data
               splitting, parameter initialization and training set shuffling
        :param debug: (bool, default: `False`) If `True` turns on `tfdbg`
                with `inf_or_nan` checks.

        # Return

        :return: (None) `None`
        """
        training_set_metadata = training_set_metadata or self.training_set_metadata
        training_dataset, _, _, training_set_metadata = preprocess_for_training(
            self.model_definition,
            training_set=dataset,
            training_set_metadata=training_set_metadata,
            data_format=data_format,
            skip_save_processed_input=True,
            preprocessing_params=self.model_definition[PREPROCESSING],
            random_seed=random_seed
        )

        if not self.training_set_metadata:
            self.training_set_metadata = training_set_metadata

        if not self.model:
            update_model_definition_with_metadata(
                self.model_definition,
                training_set_metadata
            )
            self.model = LudwigModel.create_model(self.model_definition,
                                                  random_seed=random_seed)

        if not self._online_trainer:
            self._online_trainer = Trainer(
                **self.model_definition[TRAINING],
                random_seed=random_seed,
                horoovd=self._horovod,
                debug=debug
            )

        self._online_trainer.train_online(
            self.model,
            training_dataset,
        )

    def predict(
            self,
            dataset: Union[str, dict, pd.DataFrame] = None,
            data_format: str = None,
            split: str = FULL,
            batch_size: int = 128,
            skip_save_unprocessed_output: bool = True,
            skip_save_predictions: bool = True,
            output_directory: str = 'results',
            return_type: Union[str, dict, pd.DataFrame] = pd.DataFrame,
            debug=False,
            **kwargs
    ) -> Tuple[Union[dict, pd.DataFrame], str]:
        """
        Using a trained model, make predictions from the provided dataset.

        # Inputs
        :param dataset: (Union[str, dict, pandas.DataFrame]) source containing
            the entire dataset to be evaluated.
        :param data_format: (str, default: `None`) format to interpret data
            sources. Will be inferred automatically if not specified.  Valid
            formats are `'auto'`, `'csv'`, `'df'`, `'dict'`, `'excel'`, `'feather'`,
            `'fwf'`, `'hdf5'` (cache file produced during previous training),
            `'html'` (file containing a single HTML `<table>`), `'json'`, `'jsonl'`,
            `'parquet'`, `'pickle'` (pickled Pandas DataFrame), `'sas'`, `'spss'`,
            `'stata'`, `'tsv'`.
        :param: split: (str, default= `'full'`): if the input dataset contains
            a split column, this parameter indicates which split of the data
            to use. Possible values are `'full'`, `'training'`, `
            'validation'`, `'test'`.
        :param batch_size: (int, default: 128) size of batch to use when making
            predictions.
        :param skip_save_unprocessed_output: (bool, default: `True`) if this
            parameter is `False`, predictions and their probabilities are saved
            in both raw unprocessed numpy files containing tensors and as
            postprocessed CSV files (one for each output feature).
            If this parameter is `True`, only the CSV ones are saved and the
            numpy ones are skipped.
        :param skip_save_predictions: (bool, default: `True`) skips saving
            test predictions CSV files.
        :param output_directory: (str, default: `'results'`) the directory that
            will contain the training statistics, TensorBoard logs, the saved
            model and the training progress files.
        :param return_type: (Union[str, dict, pandas.DataFrame], default: pd.DataFrame)
            indicates the format of the returned predictions.
        :param debug: (bool, default: `False`) If `True` turns on `tfdbg`
                with `inf_or_nan checks`.

        # Return

        :return: (Tuple[Union[dict, pd.DataFrame], str]) `(predictions, output_directory)`
            `predictions` predictions from the provided dataset,
            `output_directory` filepath string to where data was stored.

        """
        self._check_initialization()

        # preprocessing
        logger.debug('Preprocessing')
        dataset, training_set_metadata = preprocess_for_prediction(
            self.model_definition,
            dataset=dataset,
            training_set_metadata=self.training_set_metadata,
            data_format=data_format,
            split=split,
            include_outputs=False
        )

        logger.debug('Predicting')
        predictor = Predictor(
            batch_size=batch_size, horovod=self._horovod, debug=debug
        )
        predictions = predictor.batch_predict(
            self.model,
            dataset,
        )

        if is_on_master():
            # if we are skipping all saving,
            # there is no need to create a directory that will remain empty
            should_create_exp_dir = not (
                    skip_save_unprocessed_output and skip_save_predictions
            )
            if should_create_exp_dir:
                os.makedirs(output_directory, exist_ok=True)

        logger.debug('Postprocessing')
        postproc_predictions = convert_predictions(
            postprocess(
                predictions,
                self.model.output_features,
                self.training_set_metadata,
                output_directory=output_directory,
                skip_save_unprocessed_output=skip_save_unprocessed_output
                                             or not is_on_master(),
            ),
            self.model.output_features,
            self.training_set_metadata,
            return_type=return_type
        )

        if is_on_master():
            if not skip_save_predictions:
                save_prediction_outputs(postproc_predictions,
                                        output_directory)

                logger.info('Saved to: {0}'.format(output_directory))

        return postproc_predictions, output_directory

    def evaluate(
            self,
            dataset: Union[str, dict, pd.DataFrame] = None,
            data_format: str = None,
            split: str = FULL,
            batch_size: int = 128,
            skip_save_unprocessed_output: bool = True,
            skip_save_predictions: bool = True,
            skip_save_eval_stats: bool = True,
            collect_predictions: bool = False,
            collect_overall_stats: bool = False,
            output_directory: str = 'results',
            return_type: Union[str, dict, pd.DataFrame] = pd.DataFrame,
            debug: bool = False,
            **kwargs
    ) -> Tuple[dict, Union[dict, pd.DataFrame], str]:
        """This function is used to predict the output variables given the
        input variables using the trained model and compute test statistics
        like performance measures, confusion matrices and the like.

        # Inputs
        :param dataset: (Union[str, dict, pandas.DataFrame]) source containing
            the entire dataset to be evaluated.
        :param data_format: (str, default: `None`) format to interpret data
            sources. Will be inferred automatically if not specified.  Valid
            formats are `'auto'`, `'csv'`, `'df'`, `'dict'`, `'excel'`, `'feather'`,
            `'fwf'`, `'hdf5'` (cache file produced during previous training),
            `'html'` (file containing a single HTML `<table>`), `'json'`, `'jsonl'`,
            `'parquet'`, `'pickle'` (pickled Pandas DataFrame), `'sas'`, `'spss'`,
            `'stata'`, `'tsv'`.
        :param: split: (str, default= `'full'`): if the input dataset contains
            a split column, this parameter indicates which split of the data
            to use. Possible values are `'full'`, `'training'`, `
            'validation'`, `'test'`.
        :param batch_size: (int, default: 128) size of batch to use when making
            predictions.
        :param skip_save_unprocessed_output: (bool, default: `True`) if this
            parameter is `False`, predictions and their probabilities are saved
            in both raw unprocessed numpy files containing tensors and as
            postprocessed CSV files (one for each output feature).
            If this parameter is `True`, only the CSV ones are saved and the
            numpy ones are skipped.
        :param skip_save_predictions: (bool, default: `True`) skips saving
            test predictions CSV files.
        :param skip_save_eval_stats: (bool, default: `True`) skips saving
            test statistics JSON file.
        :param collect_predictions: (bool, default: `False`) if `True`
            collects post-processed predictions during eval.
        :param collect_overall_stats: (bool, default: False) if `True`
            collects overall stats during eval.
        :param output_directory: (str, default: `'results'`) the directory that
            will contain the training statistics, TensorBoard logs, the saved
            model and the training progress files.
        :param return_type: (Union[str, dict, pandas.DataFrame], default: pandas.DataFrame) indicates
            the format to of the returned predictions.
        :param debug: (bool, default: `False`) If `True` turns on `tfdbg`
                with `inf_or_nan` checks.


        # Return
        :return: (`evaluation_statistics`, `predictions`, `output_directory`)
            `evaluation_statistics` dictionary containing evaluation performance
                statistics,
            `postprocess_predictions` contains predicted values,
            `output_directory` is location where results are stored.

        """
        self._check_initialization()

        # preprocessing
        logger.debug('Preprocessing')
        dataset, training_set_metadata = preprocess_for_prediction(
            self.model_definition,
            dataset=dataset,
            training_set_metadata=self.training_set_metadata,
            data_format=data_format,
            split=split,
            include_outputs=True
        )

        logger.debug('Predicting')
        predictor = Predictor(
            batch_size=batch_size, horovod=self._horovod, debug=debug
        )
        eval_stats, predictions = predictor.batch_evaluation(
            self.model,
            dataset,
            collect_predictions=collect_predictions or collect_overall_stats,
        )

        # calculate the overall metrics
        if collect_overall_stats:
            overall_stats = calculate_overall_stats(
                self.model.output_features,
                predictions,
                dataset,
                training_set_metadata
            )
            eval_stats = {
                of_name: {**eval_stats[of_name], **overall_stats[of_name]}
                # account for presence of 'combined' key
                if of_name in overall_stats
                else {**eval_stats[of_name]} for of_name in eval_stats
            }

        if is_on_master():
            # if we are skipping all saving,
            # there is no need to create a directory that will remain empty
            should_create_exp_dir = not (
                    skip_save_unprocessed_output and
                    skip_save_predictions and
                    skip_save_eval_stats
            )
            if should_create_exp_dir:
                os.makedirs(output_directory, exist_ok=True)

        if collect_predictions:
            logger.debug('Postprocessing')
            postproc_predictions = postprocess(
                predictions,
                self.model.output_features,
                self.training_set_metadata,
                output_directory=output_directory,
                skip_save_unprocessed_output=skip_save_unprocessed_output
                                             or not is_on_master(),
            )
        else:
            postproc_predictions = predictions  # = {}

        if is_on_master():
            should_save_predictions = (
                    collect_predictions
                    and postproc_predictions is not None
                    and not skip_save_predictions
            )
            if should_save_predictions:
                save_prediction_outputs(postproc_predictions, output_directory)

            print_evaluation_stats(eval_stats)
            if not skip_save_eval_stats:
                save_evaluation_stats(eval_stats, output_directory)

            if should_save_predictions or not skip_save_eval_stats:
                logger.info('Saved to: {0}'.format(output_directory))

        if collect_predictions:
            postproc_predictions = convert_predictions(
                postproc_predictions,
                self.model.output_features,
                self.training_set_metadata,
                return_type=return_type)

        return eval_stats, postproc_predictions, output_directory

    def experiment(
            self,
            dataset: Union[str, dict, pd.DataFrame] = None,
            training_set: Union[str, dict, pd.DataFrame] = None,
            validation_set: Union[str, dict, pd.DataFrame] = None,
            test_set: Union[str, dict, pd.DataFrame] = None,
            training_set_metadata: Union[str, dict] = None,
            data_format: str = None,
            experiment_name: str = 'experiment',
            model_name: str = 'run',
            model_load_path: str = None,
            model_resume_path: str = None,
            eval_split: str = TEST,
            skip_save_training_description: bool = False,
            skip_save_training_statistics: bool = False,
            skip_save_model: bool = False,
            skip_save_progress: bool = False,
            skip_save_log: bool = False,
            skip_save_processed_input: bool = False,
            skip_save_unprocessed_output: bool = False,
            skip_save_predictions: bool = False,
            skip_save_eval_stats: bool = False,
            skip_collect_predictions: bool = False,
            skip_collect_overall_stats: bool = False,
            output_directory: str = 'results',
            random_seed: int = default_random_seed,
            debug: bool = False,
            **kwargs
    ) -> Tuple[Optional[dict], dict, Union[dict, pd.DataFrame], str]:
        """
        Trains a model on a dataset's training and validation splits and
        uses it to predict on the test split.
        It saves the trained model and the statistics of training and testing.

        # Inputs
        :param dataset: (Union[str, dict, pandas.DataFrame], default: `None`)
            source containing the entire dataset to be used in the experiment.
            If it has a split column, it will be used for splitting (0 for train,
            1 for validation, 2 for test), otherwise the dataset will be
            randomly split.
        :param training_set: (Union[str, dict, pandas.DataFrame], default: `None`)
            source containing training data.
        :param validation_set: (Union[str, dict, pandas.DataFrame], default: `None`)
            source containing validation data.
        :param test_set: (Union[str, dict, pandas.DataFrame], default: `None`)
            source containing test data.
        :param training_set_metadata: (Union[str, dict], default: `None`)
            metadata JSON file or loaded metadata.  Intermediate preprocess
            structure containing the mappings of the input
            dataset created the first time an input file is used in the same
            directory with the same name and a '.meta.json' extension.
        :param data_format: (str, default: `None`) format to interpret data
            sources. Will be inferred automatically if not specified.  Valid
            formats are `'auto'`, `'csv'`, `'df'`, `'dict'`, `'excel'`, `'feather'`,
            `'fwf'`, `'hdf5'` (cache file produced during previous training),
            `'html'` (file containing a single HTML `<table>`), `'json'`, `'jsonl'`,
            `'parquet'`, `'pickle'` (pickled Pandas DataFrame), `'sas'`, `'spss'`,
            `'stata'`, `'tsv'`.
        :param experiment_name: (str, default: `'experiment'`) name for
            the experiment.
        :param model_name: (str, default: `'run'`) name of the model that is
            being used.
        :param model_load_path: (str, default: `None`) if this is specified the
            loaded model will be used as initialization
            (useful for transfer learning).
        :param model_resume_path: (str, default: `None`) resumes training of
            the model from the path specified. The model definition is restored.
            In addition to model definition, training statistics and loss for
            epoch and the state of the optimizer are restored such that
            training can be effectively continued from a previously interrupted
            training process.
        :param eval_split: (str, default: `test`) split on which
            to perform evaluation. Valid values are `training`, `validation`
            and `test`.
        :param skip_save_training_description: (bool, default: `False`) disables
            saving the description JSON file.
        :param skip_save_training_statistics: (bool, default: `False`) disables
            saving training statistics JSON file.
        :param skip_save_model: (bool, default: `False`) disables
            saving model weights and hyperparameters each time the model
            improves. By default Ludwig saves model weights after each epoch
            the validation metric improves, but if the model is really big
            that can be time consuming if you do not want to keep
            the weights and just find out what performance can a model get
            with a set of hyperparameters, use this parameter to skip it,
            but the model will not be loadable later on and the returned model
            will have the weights obtained at the end of training, instead of
            the weights of the epoch with the best validation performance.
        :param skip_save_progress: (bool, default: `False`) disables saving
            progress each epoch. By default Ludwig saves weights and stats
            after each epoch for enabling resuming of training, but if
            the model is really big that can be time consuming and will uses
            twice as much space, use this parameter to skip it, but training
            cannot be resumed later on.
        :param skip_save_log: (bool, default: `False`) disables saving
            TensorBoard logs. By default Ludwig saves logs for the TensorBoard,
            but if it is not needed turning it off can slightly increase the
            overall speed.
        :param skip_save_processed_input: (bool, default: `False`) if input
            dataset is provided it is preprocessed and cached by saving an HDF5
            and JSON files to avoid running the preprocessing again. If this
            parameter is `False`, the HDF5 and JSON file are not saved.
        :param skip_save_unprocessed_output: (bool, default: `False`) by default
            predictions and their probabilities are saved in both raw
            unprocessed numpy files containing tensors and as postprocessed
            CSV files (one for each output feature). If this parameter is True,
            only the CSV ones are saved and the numpy ones are skipped.
        :param skip_save_predictions: (bool, default: `False`) skips saving test
            predictions CSV files
        :param skip_save_eval_stats: (bool, default: `False`) skips saving test
            statistics JSON file
        :param skip_collect_predictions: (bool, default: `False`) skips
            collecting post-processed predictions during eval.
        :param skip_collect_overall_stats: (bool, default: `False`) skips
            collecting overall stats during eval.
        :param output_directory: (str, default: `'results'`) the directory that
            will contain the training statistics, TensorBoard logs, the saved
            model and the training progress files.
        :param gpus: (list, default: `None`) list of GPUs that are available
            for training.
        :param gpu_memory_limit: (int, default: `None`) maximum memory in MB to
            allocate per GPU device.
        :param allow_parallel_threads: (bool, default: `True`) allow TensorFlow
            to use multithreading parallelism to improve performance at
            the cost of determinism.
        :param use_horovod: (bool, default: `None`) flag for using horovod.
        :param random_seed: (int: default: 42) random seed used for weights
            initialization, splits and any other random function.
        :param debug: (bool, default: `False) if `True` turns on `tfdbg` with
            `inf_or_nan` checks.

        # Return
        :return: (Tuple[dict, dict, tuple, str)) `(evaluation_statistics, training_statistics, preprocessed_data, output_directory)`
            `evaluation_statistics` dictionary with evaluation performance
                statistics on the test_set,
            `training_statistics` is a dictionary of training statistics for
                each output
            feature containing loss and metrics values for each epoch,
            `preprocessed_data` tuple containing preprocessed
            `(training_set, validation_set, test_set)`, `output_directory`
            filepath string to where results are stored.

        """
        (
            train_stats,
            preprocessed_data,
            output_directory
        ) = self.train(
            dataset=dataset,
            training_set=training_set,
            validation_set=validation_set,
            test_set=test_set,
            training_set_metadata=training_set_metadata,
            data_format=data_format,
            experiment_name=experiment_name,
            model_name=model_name,
            model_load_path=model_load_path,
            model_resume_path=model_resume_path,
            skip_save_training_description=skip_save_training_description,
            skip_save_training_statistics=skip_save_training_statistics,
            skip_save_model=skip_save_model,
            skip_save_progress=skip_save_progress,
            skip_save_log=skip_save_log,
            skip_save_processed_input=skip_save_processed_input,
            skip_save_unprocessed_output=skip_save_unprocessed_output,
            output_directory=output_directory,
            random_seed=random_seed,
            debug=debug,
        )

        (training_set,
         validation_set,
         test_set,
         training_set_metadata) = preprocessed_data

        eval_set = validation_set
        if eval_split == TRAINING:
            eval_set = training_set
        elif eval_split == VALIDATION:
            eval_set = validation_set
        elif eval_split == TEST:
            eval_set = test_set
        else:
            logger.warning(f"Eval split {eval_split} not supported. "
                           f"Using validation set instead")

        if eval_set is not None:
            if self.model_definition[TRAINING]['eval_batch_size'] > 0:
                batch_size = self.model_definition[TRAINING]['eval_batch_size']
            else:
                batch_size = self.model_definition[TRAINING]['batch_size']

            # predict
            eval_stats, _, _ = self.evaluate(
                eval_set,
                data_format=data_format,
                batch_size=batch_size,
                output_directory=output_directory,
                skip_save_unprocessed_output=skip_save_unprocessed_output,
                skip_save_predictions=skip_save_predictions,
                skip_save_eval_stats=skip_save_eval_stats,
                collect_predictions=not skip_collect_predictions,
                collect_overall_stats=not skip_collect_overall_stats,
                return_type='dict',
                debug=debug
            )
        else:
            logger.warning(f"The evaluation set {eval_set} was not provided. "
                           f"Skipping evaluation")
            eval_stats = None

        return eval_stats, train_stats, preprocessed_data, output_directory

    def collect_weights(
            self,
            tensor_names: List[str] = None,
            **kwargs
    ) -> list:
        """Load a pre-trained model and collect the tensors with a specific name

        # Inputs
        :param tensor_names: (list, default: `None`) List of tensor names to collect
            weights

        # Return
        :return: (list) List of tensors

        """
        self._check_initialization()
        collected_tensors = self.model.collect_weights(tensor_names)
        return collected_tensors

    def collect_activations(
            self,
            layer_names: List[str],
            dataset: Union[str, Dict[str, list], pd.DataFrame],
            data_format: str = None,
            split: str = FULL,
            batch_size: int = 128,
            debug: bool = False,
            **kwargs
    ) -> list:
        """Loads a pre-trained model model and input data to collect the values
        of the activations contained in the tensors.

        # Inputs
        :param layer_names: (list) list of strings for layer names in the model
            to collect activations.
        :param dataset: (Union[str, Dict[str, list], pandas.DataFrame]) source
            containing the data to make predictions.
        :param data_format: (str, default: `None`) format to interpret data
            sources. Will be inferred automatically if not specified.  Valid
            formats are `'auto'`, `'csv'`, `'df'`, `'dict'`, `'excel'`, `'feather'`,
            `'fwf'`, `'hdf5'` (cache file produced during previous training),
            `'html'` (file containing a single HTML `<table>`), `'json'`, `'jsonl'`,
            `'parquet'`, `'pickle'` (pickled Pandas DataFrame), `'sas'`, `'spss'`,
            `'stata'`, `'tsv'`.
        :param: split: (str, default= `'full'`): if the input dataset contains
            a split column, this parameter indicates which split of the data
            to use. Possible values are `'full'`, `'training'`, `
            'validation'`, `'test'`.
        :param batch_size: (int, default: 128) size of batch to use when making
            predictions.
        :param debug: (bool, default: `False`) if `True` turns on `tfdbg`
            with `inf_or_nan` checks.

        # Return
        :return: (list) list of collected tensors.

        """
        self._check_initialization()

        # preprocessing
        logger.debug('Preprocessing')
        dataset, training_set_metadata = preprocess_for_prediction(
            self.model_definition,
            dataset=dataset,
            training_set_metadata=self.training_set_metadata,
            data_format=data_format,
            split=split,
            include_outputs=False
        )

        logger.debug('Predicting')
        predictor = Predictor(
            batch_size=batch_size, horovod=self._horovod, debug=debug
        )
        activations = predictor.batch_collect_activations(
            self.model,
            layer_names,
            dataset,
        )

        return activations

    @staticmethod
    def load(
            model_dir: str,
            logging_level: int = logging.ERROR,
            use_horovod: bool = None,
            gpus: Union[str, int, List[int]] = None,
            gpu_memory_limit: int = None,
            allow_parallel_threads: bool = True
    ) -> 'LudwigModel':  # return is an instance of ludwig.api.LudwigModel class
        """This function allows for loading pretrained models

        # Inputs

        :param model_dir: (str) path to the directory containing the model.
               If the model was trained by the `train` or `experiment` command,
               the model is in `results_dir/experiment_dir/model`.
        :param logging_level: (int, default: 40) log level that will be sent to
            stderr.
        :param use_horovod: (bool, default: `None`) use Horovod for distributed
            training. Will be set
            automatically if `horovodrun` is used to launch the training script.
        :param gpus: (Union[str, int, List[int]], default: `None`) GPUs
            to use (it uses the same syntax of CUDA_VISIBLE_DEVICES)
        :param gpu_memory_limit: (int: default: `None`) maximum memory in MB to
            allocate per GPU device.
        :param allow_parallel_threads: (bool, default: `True`) allow TensorFlow
            to use
            multithreading parallelism to improve performance at the cost of
            determinism.

        # Return

        :return: (LudwigModel) a LudwigModel object


        # Example usage

        ```python
        ludwig_model = LudwigModel.load(model_dir)
        ```

        """
        horovod = configure_horovod(use_horovod)
        model_definition = broadcast_return(lambda: load_json(os.path.join(
            model_dir,
            MODEL_HYPERPARAMETERS_FILE_NAME
        )), horovod)

        # initialize model
        ludwig_model = LudwigModel(
            model_definition,
            logging_level=logging_level,
            use_horovod=use_horovod,
            gpus=gpus,
            gpu_memory_limit=gpu_memory_limit,
            allow_parallel_threads=allow_parallel_threads,
        )

        # generate model from definition
        ludwig_model.model = LudwigModel.create_model(model_definition)

        # load model weights
        ludwig_model.load_weights(model_dir)

        # load train set metadata
        ludwig_model.training_set_metadata = broadcast_return(
            lambda: load_metadata(
                os.path.join(
                    model_dir,
                    TRAIN_SET_METADATA_FILE_NAME
                )
            ), horovod
        )

        return ludwig_model

    def load_weights(
            self,
            model_dir: str
    ) -> None:
        """
        Loads weights from a pre-trained model

        # Inputs
        :param model_dir: (str) filepath string to location of a pre-trained
            model

        # Return
        :return: `None`

        # Example usage

        ```python
        ludwig_model.load_weights(model_dir)
        ```

        """
        if is_on_master():
            weights_save_path = os.path.join(
                model_dir,
                MODEL_WEIGHTS_FILE_NAME
            )
            self.model.load_weights(weights_save_path)

        if self._horovod:
            # Model weights are only saved on master, so broadcast
            # to all other ranks
            self._horovod.broadcast_variables(self.model.variables,
                                              root_rank=0)

    def save(
            self,
            save_path: str
    ) -> None:
        """This function allows to save models on disk

        # Inputs

        :param  save_path: (str) path to the directory where the model is
                going to be saved. Both a JSON file containing the model
                architecture hyperparameters and checkpoints files containing
                model weights will be saved.

        # Return

        :return: (None) `None`

        # Example usage

        ```python
        ludwig_model.save(save_path)
        ```

        """
        self._check_initialization()

        # save model definition
        self.save_model_definition(save_path)

        # save model weights
        model_weights_path = os.path.join(save_path, MODEL_WEIGHTS_FILE_NAME)
        self.model.save_weights(model_weights_path)

        # save training set metadata
        training_set_metadata_path = os.path.join(
            save_path,
            TRAIN_SET_METADATA_FILE_NAME
        )
        save_json(training_set_metadata_path, self.training_set_metadata)

    def save_model_definition(
            self,
            save_path: str
    ) -> None:
        """
        Save model definition to specoficed location.

        # Inputs

        :param save_path: (str) filepath string to save model definition as a
            JSON file.

        # Return
        :return: `None`

        """
        os.makedirs(save_path, exist_ok=True)
        model_hyperparameters_path = os.path.join(
            save_path,
            MODEL_HYPERPARAMETERS_FILE_NAME
        )
        save_json(model_hyperparameters_path, self.model_definition)

    def save_savedmodel(self, save_path: str) -> None:
        """This function allows to save models on disk

        # Inputs

        :param  save_path: (str) path to the directory where the SavedModel
                is going to be saved.

        # Return

        :return: `None`

        # Example usage

        ```python
        ludwig_model.save_for_serving(save_path)
        ```

        """
        self._check_initialization()
        self.model.save_savedmodel(save_path)

    def _check_initialization(self):
        if self.model is None or \
                self.model_definition is None or \
                self.training_set_metadata is None:
            raise ValueError('Model has not been trained or loaded')

    @staticmethod
    def create_model(
            model_definition: dict,
            random_seed: int = default_random_seed
    ) -> ECD:
        """Instantiates Encoder-Combiner-Decoder (ECD) object

        # Inputs
        :param model_definition: (dict) Ludwig model definition
        :param random_seed: (int, default: ludwig default random seed) Random
            seed used for weights initialization,
            splits and any other random function.

        # Return
        :return: (ludwig.models.ECD) Instance of the Ludwig model object.

        """
        # todo: support loading other model types based on definition
        return ECD(
            input_features_def=model_definition['input_features'],
            combiner_def=model_definition['combiner'],
            output_features_def=model_definition['output_features'],
            random_seed=random_seed,
        )

    @staticmethod
    def set_logging_level(logging_level: int) -> None:
        """
        Sets level for log messages.

        # Inputs

        :param logging_level: (int) Set/Update the logging level. Use logging
        constants like `logging.DEBUG` , `logging.INFO` and `logging.ERROR`.

        # Return

        :return: `None`
        """
        logging.getLogger('ludwig').setLevel(logging_level)
        if logging_level in {logging.WARNING, logging.ERROR, logging.CRITICAL}:
            set_disable_progressbar(True)
        else:
            set_disable_progressbar(False)


def kfold_cross_validate(
        num_folds: int,
        model_definition: Union[dict, str],
        dataset: str = None,
        data_format: str = None,
        skip_save_training_description: bool = False,
        skip_save_training_statistics: bool = False,
        skip_save_model: bool = False,
        skip_save_progress: bool = False,
        skip_save_log: bool = False,
        skip_save_processed_input: bool = False,
        skip_save_predictions: bool = False,
        skip_save_eval_stats: bool = False,
        skip_collect_predictions: bool = False,
        skip_collect_overall_stats: bool = False,
        output_directory: str = 'results',
        random_seed: int = default_random_seed,
        gpus: Union[str, int, List[int]] = None,
        gpu_memory_limit: int = None,
        allow_parallel_threads: bool = True,
        use_horovod: bool = None,
        logging_level: int = logging.INFO,
        debug: bool = False,
        **kwargs
) -> Tuple[dict, dict]:
    """Performs k-fold cross validation and returns result data structures.

    # Inputs

    :param num_folds: (int) number of folds to create for the cross-validation
    :param model_definition: (Union[dict, str]) model specification
           required to build a model. Parameter may be a dictionary or string
           specifying the file path to a yaml configuration file.  Refer to the
           [User Guide](http://ludwig.ai/user_guide/#model-definition)
           for details.
    :param dataset: (Union[str, dict, pandas.DataFrame], default: `None`)
        source containing the entire dataset to be used for k_fold processing.
        :param data_format: (str, default: `None`) format to interpret data
            sources. Will be inferred automatically if not specified.  Valid
            formats are `'auto'`, `'csv'`, `'df'`, `'dict'`, `'excel'`, `'feather'`,
            `'fwf'`,
            `'html'` (file containing a single HTML `<table>`), `'json'`, `'jsonl'`,
            `'parquet'`, `'pickle'` (pickled Pandas DataFrame), `'sas'`, `'spss'`,
            `'stata'`, `'tsv'`.  Currenlty `hdf5` format is not supported for
            k_fold cross validation.
    :param skip_save_training_description: (bool, default: `False`) disables
            saving the description JSON file.
    :param skip_save_training_statistics: (bool, default: `False`) disables
            saving training statistics JSON file.
    :param skip_save_model: (bool, default: `False`) disables
        saving model weights and hyperparameters each time the model
        improves. By default Ludwig saves model weights after each epoch
        the validation metric improves, but if the model is really big
        that can be time consuming if you do not want to keep
        the weights and just find out what performance can a model get
        with a set of hyperparameters, use this parameter to skip it,
        but the model will not be loadable later on and the returned model
        will have the weights obtained at the end of training, instead of
        the weights of the epoch with the best validation performance.
    :param skip_save_progress: (bool, default: `False`) disables saving
           progress each epoch. By default Ludwig saves weights and stats
           after each epoch for enabling resuming of training, but if
           the model is really big that can be time consuming and will uses
           twice as much space, use this parameter to skip it, but training
           cannot be resumed later on.
    :param skip_save_log: (bool, default: `False`) disables saving TensorBoard
           logs. By default Ludwig saves logs for the TensorBoard, but if it
           is not needed turning it off can slightly increase the
           overall speed.
    :param skip_save_processed_input: (bool, default: `False`) if input
        dataset is provided it is preprocessed and cached by saving an HDF5
        and JSON files to avoid running the preprocessing again. If this
        parameter is `False`, the HDF5 and JSON file are not saved.
    :param skip_save_predictions: (bool, default: `False`) skips saving test
            predictions CSV files.
    :param skip_save_eval_stats: (bool, default: `False`) skips saving test
            statistics JSON file.
    :param skip_collect_predictions: (bool, default: `False`) skips collecting
            post-processed predictions during eval.
    :param skip_collect_overall_stats: (bool, default: `False`) skips collecting
            overall stats during eval.
    :param output_directory: (str, default: `'results'`) the directory that
        will contain the training statistics, TensorBoard logs, the saved
        model and the training progress files.
    :param random_seed: (int, default: `42`) Random seed
            used for weights initialization,
           splits and any other random function.
    :param gpus: (list, default: `None`) list of GPUs that are available
            for training.
    :param gpu_memory_limit: (int, default: `None`) maximum memory in MB to
            allocate per GPU device.
    :param allow_parallel_threads: (bool, default: `True`) allow TensorFlow to
            use multithreading parallelism
           to improve performance at the cost of determinism.
    :param use_horovod: (bool, default: `None`) flag for using horovod
    :param debug: (bool, default: `False`) If `True` turns on tfdbg
            with `inf_or_nan` checks.
    :param logging_level: (int, default: INFO) log level to send to stderr.


    # Return

    :return: (tuple(kfold_cv_statistics, kfold_split_indices), dict) a tuple of
            dictionaries `kfold_cv_statistics`: contains metrics from cv run.
             `kfold_split_indices`: indices to split training data into
             training fold and test fold.
    """
    set_on_master(use_horovod)

    # if model definition is a path, convert to dictionary
    if isinstance(model_definition, str):  # assume path
        with open(model_definition, 'r') as def_file:
            model_definition = yaml.safe_load(def_file)

    # check for k_fold
    if num_folds is None:
        raise ValueError(
            'k_fold parameter must be specified'
        )

    logger.info('starting {:d}-fold cross validation'.format(num_folds))

    # create output_directory if not available
    if not os.path.isdir(output_directory):
        os.mkdir(output_directory)

    # prepare data for k-fold processing
    # use Ludwig's utility to facilitate creating a dataframe
    # that is used as the basis for creating folds

    # determine data format of provided dataset
    if not data_format or data_format == 'auto':
        data_format = figure_data_format(dataset)

    # use appropriate reader to create dataframe
    if data_format in DATAFRAME_FORMATS:
        data_df = dataset
        data_dir = os.getcwd()
    elif data_format in DICT_FORMATS:
        data_df = pd.DataFrame(dataset)
        data_dir = os.getcwd()
    elif data_format in CACHEABLE_FORMATS:
        data_reader = get_from_registry(data_format,
                                        external_data_reader_registry)
        data_df = data_reader(dataset)
        data_dir = os.path.dirname(dataset)
    else:
        ValueError(
            "{} format is not supported for k_fold_cross_validate()"
                .format(data_format)
        )

    kfold_cv_stats = {}
    kfold_split_indices = {}

    for train_indices, test_indices, fold_num in \
            generate_kfold_splits(data_df, num_folds, random_seed):
        with tempfile.TemporaryDirectory(dir=data_dir) as temp_dir_name:
            curr_train_df = data_df.iloc[train_indices]
            curr_test_df = data_df.iloc[test_indices]

            kfold_split_indices['fold_' + str(fold_num)] = {
                'training_indices': train_indices,
                'test_indices': test_indices
            }

            # train and validate model on this fold
            logger.info("training on fold {:d}".format(fold_num))

            model = LudwigModel(
                model_definition=model_definition,
                logging_level=logging_level,
                use_horovod=use_horovod,
                gpus=gpus,
                gpu_memory_limit=gpu_memory_limit,
                allow_parallel_threads=allow_parallel_threads,
            )
            (
                eval_stats,
                train_stats,
                preprocessed_data,
                output_directory
            ) = model.experiment(
                training_set=curr_train_df,
                test_set=curr_test_df,
                experiment_name='cross_validation',
                model_name='fold_' + str(fold_num),
                skip_save_training_description=skip_save_training_description,
                skip_save_training_statistics=skip_save_training_statistics,
                skip_save_model=skip_save_model,
                skip_save_progress=skip_save_progress,
                skip_save_log=skip_save_log,
                skip_save_processed_input=skip_save_processed_input,
                skip_save_predictions=skip_save_predictions,
                skip_save_eval_stats=skip_save_eval_stats,
                skip_collect_predictions=skip_collect_predictions,
                skip_collect_overall_stats=skip_collect_overall_stats,
                output_directory=os.path.join(temp_dir_name, 'results'),
                random_seed=random_seed,
                debug=debug,
            )

            # augment the training statistics with scoring metric from
            # the hold out fold
            train_stats['fold_eval_stats'] = eval_stats

            # collect training statistics for this fold
            kfold_cv_stats['fold_' + str(fold_num)] = train_stats

    # consolidate raw fold metrics across all folds
    raw_kfold_stats = {}
    for fold_name in kfold_cv_stats:
        curr_fold_eval_stats = kfold_cv_stats[fold_name]['fold_eval_stats']
        for of_name in curr_fold_eval_stats:
            if of_name not in raw_kfold_stats:
                raw_kfold_stats[of_name] = {}
            fold_eval_stats_of = curr_fold_eval_stats[of_name]

            for metric in fold_eval_stats_of:
                if metric not in {
                    'predictions',
                    'probabilities',
                    'confusion_matrix',
                    'overall_stats',
                    'per_class_stats',
                    'roc_curve',
                    'precision_recall_curve'
                }:
                    if metric not in raw_kfold_stats[of_name]:
                        raw_kfold_stats[of_name][metric] = []
                    raw_kfold_stats[of_name][metric].append(
                        fold_eval_stats_of[metric]
                    )

    # calculate overall kfold statistics
    overall_kfold_stats = {}
    for of_name in raw_kfold_stats:
        overall_kfold_stats[of_name] = {}
        for metric in raw_kfold_stats[of_name]:
            mean = np.mean(raw_kfold_stats[of_name][metric])
            std = np.std(raw_kfold_stats[of_name][metric])
            overall_kfold_stats[of_name][metric + '_mean'] = mean
            overall_kfold_stats[of_name][metric + '_std'] = std

    kfold_cv_stats['overall'] = overall_kfold_stats

    logger.info('completed {:d}-fold cross validation'.format(num_folds))

    return kfold_cv_stats, kfold_split_indices
