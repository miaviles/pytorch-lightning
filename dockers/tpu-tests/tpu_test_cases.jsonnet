local base = import 'templates/base.libsonnet';
local tpus = import 'templates/tpus.libsonnet';
local utils = import "templates/utils.libsonnet";

local tputests = base.BaseTest {
  frameworkPrefix: 'pl',
  modelName: 'tpu-tests',
  mode: 'postsubmit',
  configMaps: [],

  timeout: 1800, # 30 minutes, in seconds.

  image: std.extVar('image'),
  imageTag: std.extVar('image-tag'),

  tpuSettings+: {
    softwareVersion: 'pytorch-VERSION',
  },
  accelerator: tpus.v3_8,

  command: utils.scriptCommand(
    |||
      export MKL_THREADING_LAYER=GNU  # todo: drop after new XLA docker is published
      cd pytorch-lightning
      coverage run --source=pytorch_lightning -m pytest pytorch_lightning tests -v
      test_exit_code=$?
      echo "\n||| END PYTEST LOGS |||\n"
      coverage xml
      cat coverage.xml | tr -d '\t'
      test $test_exit_code -eq 0
    |||
  ),
};

tputests.oneshotJob
