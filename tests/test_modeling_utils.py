# Copyright 2022 The HuggingFace Team. All rights reserved.
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

import json
import os
import tempfile
import unittest
import warnings
from collections import OrderedDict
from typing import Optional

import torch
import torch.nn as nn
from parameterized import parameterized
from safetensors.torch import save_file

from accelerate import init_empty_weights
from accelerate.big_modeling import cpu_offload
from accelerate.test_utils import (
    require_huggingface_suite,
    require_multi_device,
    require_non_cpu,
    require_non_hpu,
    torch_device,
)
from accelerate.utils.modeling import (
    align_module_device,
    check_device_map,
    clean_device_map,
    compute_module_sizes,
    compute_module_total_buffer_size,
    convert_file_size_to_int,
    find_tied_parameters,
    get_balanced_memory,
    get_module_size_with_ties,
    get_state_dict_offloaded_model,
    infer_auto_device_map,
    load_checkpoint_in_model,
    load_state_dict,
    named_module_tensors,
    retie_parameters,
    set_module_tensor_to_device,
)


torch_device = f"{torch_device}:0" if torch_device != "cpu" else "cpu"


class ModelForTest(nn.Module):
    def __init__(self):
        super().__init__()
        self.linear1 = nn.Linear(3, 4)
        self.batchnorm = nn.BatchNorm1d(4)
        self.linear2 = nn.Linear(4, 5)

    def forward(self, x):
        return self.linear2(self.batchnorm(self.linear1(x)))


class NestedModelForTest(nn.Module):
    def __init__(self):
        super().__init__()
        self.model = ModelForTest()

    def forward(self, x):
        return self.model(x)


class LinearWithNonPersistentBuffers(nn.Module):
    def __init__(self, in_features: int, out_features: int, bias: bool = True, device=None, dtype=None) -> None:
        factory_kwargs = {"device": device, "dtype": dtype}
        super().__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.register_buffer("weight", torch.empty((out_features, in_features), **factory_kwargs))
        if bias:
            self.register_buffer("bias", torch.empty(out_features, **factory_kwargs), persistent=False)
        else:
            self.register_buffer("bias", None)

    def forward(self, input: torch.Tensor) -> torch.Tensor:
        return torch.nn.functional.linear(input, self.weight, self.bias)


class ModelSeveralDtypes(nn.Module):
    def __init__(self):
        super().__init__()
        self.register_buffer("int_param", torch.randint(high=10, size=(15, 30)))
        self.register_parameter("float_param", torch.nn.Parameter(torch.rand(10, 5)))

    def forward(self, x):
        return x + 2


def sequential_model(num_layers):
    layers = OrderedDict([(f"linear{i}", nn.Linear(1000, 1000)) for i in range(1, num_layers + 1)])
    return nn.Sequential(layers)


class ModelingUtilsTester(unittest.TestCase):
    def check_set_module_tensor_for_device(self, model, device1, device2):
        assert model.linear1.weight.device == torch.device(device1)

        with self.subTest("Access by submodule and direct name for a parameter"):
            set_module_tensor_to_device(model.linear1, "weight", device2)
            assert model.linear1.weight.device == torch.device(device2)

            if torch.device(device2) == torch.device("meta"):
                with self.assertRaises(ValueError):
                    # We need a `value` to set the weight back on device1
                    set_module_tensor_to_device(model.linear1, "weight", device1)

                set_module_tensor_to_device(model.linear1, "weight", device1, value=torch.randn(4, 3))
            else:
                set_module_tensor_to_device(model.linear1, "weight", device1)
            assert model.linear1.weight.device == torch.device(device1)

        with self.subTest("Access by module and full name for a parameter"):
            set_module_tensor_to_device(model, "linear1.weight", device2)
            assert model.linear1.weight.device == torch.device(device2)

            if torch.device(device2) == torch.device("meta"):
                with self.assertRaises(ValueError):
                    # We need a `value` to set the weight back on device1
                    set_module_tensor_to_device(model, "linear1.weight", device1)
                set_module_tensor_to_device(model, "linear1.weight", device1, value=torch.randn(4, 3))
            else:
                set_module_tensor_to_device(model, "linear1.weight", device1)
            assert model.linear1.weight.device == torch.device(device1)

        assert model.batchnorm.running_mean.device == torch.device(device1)

        with self.subTest("Access by submodule and direct name for a buffer"):
            set_module_tensor_to_device(model.batchnorm, "running_mean", device2)
            assert model.batchnorm.running_mean.device == torch.device(device2)

            if torch.device(device2) == torch.device("meta"):
                with self.assertRaises(ValueError):
                    # We need a `value` to set the weight back on device1
                    set_module_tensor_to_device(model.batchnorm, "running_mean", device1)
                set_module_tensor_to_device(model.batchnorm, "running_mean", device1, value=torch.randn(4))
            else:
                set_module_tensor_to_device(model.batchnorm, "running_mean", device1)
            assert model.batchnorm.running_mean.device == torch.device(device1)

        with self.subTest("Access by module and full name for a parameter"):
            set_module_tensor_to_device(model, "batchnorm.running_mean", device2)
            assert model.batchnorm.running_mean.device == torch.device(device2)

            if torch.device(device2) == torch.device("meta"):
                with self.assertRaises(ValueError):
                    # We need a `value` to set the weight back on CPU
                    set_module_tensor_to_device(model, "batchnorm.running_mean", device1)

                set_module_tensor_to_device(model, "batchnorm.running_mean", device1, value=torch.randn(4))
            else:
                set_module_tensor_to_device(model, "batchnorm.running_mean", device1)
            assert model.batchnorm.running_mean.device == torch.device(device1)

    def test_set_module_tensor_to_meta_and_cpu(self):
        model = ModelForTest()
        self.check_set_module_tensor_for_device(model, "cpu", "meta")

    @require_non_cpu
    def test_set_module_tensor_to_cpu_and_gpu(self):
        model = ModelForTest()
        self.check_set_module_tensor_for_device(model, "cpu", torch_device)

    @require_non_cpu
    def test_set_module_tensor_to_meta_and_gpu(self):
        model = ModelForTest().to(torch_device)
        self.check_set_module_tensor_for_device(model, torch_device, "meta")

    @require_non_hpu  # hpu does not support device indexing "hpu:1"
    @require_multi_device
    def test_set_module_tensor_between_gpus(self):
        model = ModelForTest().to(torch_device)
        self.check_set_module_tensor_for_device(model, torch_device, torch_device.replace("0", "1"))

    def test_set_module_tensor_sets_dtype(self):
        model = ModelForTest()
        set_module_tensor_to_device(model, "linear1.weight", "cpu", value=model.linear1.weight, dtype=torch.float16)
        assert model.linear1.weight.dtype == torch.float16

    def test_set_module_tensor_checks_shape(self):
        model = ModelForTest()
        tensor = torch.zeros((2, 2))
        with self.assertRaises(ValueError) as cm:
            set_module_tensor_to_device(model, "linear1.weight", "cpu", value=tensor)
        assert (
            str(cm.exception)
            == 'Trying to set a tensor of shape torch.Size([2, 2]) in "weight" (which has shape torch.Size([4, 3])), this looks incorrect.'
        )

    def test_named_tensors(self):
        model = nn.BatchNorm1d(4)
        named_tensors = named_module_tensors(model)
        assert [name for name, _ in named_tensors] == [
            "weight",
            "bias",
            "running_mean",
            "running_var",
            "num_batches_tracked",
        ]

        named_tensors = named_module_tensors(model, include_buffers=False)
        assert [name for name, _ in named_tensors] == ["weight", "bias"]

        model = ModelForTest()
        named_tensors = named_module_tensors(model)
        assert [name for name, _ in named_tensors] == []

        named_tensors = named_module_tensors(model, recurse=True)
        assert [name for name, _ in named_tensors] == [
            "linear1.weight",
            "linear1.bias",
            "batchnorm.weight",
            "batchnorm.bias",
            "linear2.weight",
            "linear2.bias",
            "batchnorm.running_mean",
            "batchnorm.running_var",
            "batchnorm.num_batches_tracked",
        ]

        named_tensors = named_module_tensors(model, include_buffers=False, recurse=True)
        assert [name for name, _ in named_tensors] == [
            "linear1.weight",
            "linear1.bias",
            "batchnorm.weight",
            "batchnorm.bias",
            "linear2.weight",
            "linear2.bias",
        ]

        model = LinearWithNonPersistentBuffers(10, 10)

        named_tensors = named_module_tensors(model, include_buffers=True, remove_non_persistent=False)
        assert [name for name, _ in named_tensors] == ["weight", "bias"]

        named_tensors = named_module_tensors(model, include_buffers=True, remove_non_persistent=True)
        assert [name for name, _ in named_tensors] == ["weight"]

    def test_find_tied_parameters(self):
        model = sequential_model(4)
        assert find_tied_parameters(model) == []

        model.linear2.weight = model.linear1.weight
        assert find_tied_parameters(model) == [["linear1.weight", "linear2.weight"]]

        model.linear4.weight = model.linear1.weight
        assert find_tied_parameters(model) == [["linear1.weight", "linear2.weight", "linear4.weight"]]

        model = sequential_model(5)
        model.linear1.weight = model.linear4.weight
        model.linear2.weight = model.linear3.weight
        model.linear5.weight = model.linear2.weight
        tied_params = sorted(find_tied_parameters(model), key=lambda x: len(x))
        assert tied_params == [
            ["linear1.weight", "linear4.weight"],
            ["linear2.weight", "linear3.weight", "linear5.weight"],
        ]

        model = nn.Sequential(OrderedDict([("block1", sequential_model(4)), ("block2", sequential_model(4))]))
        model.block1.linear1.weight = model.block2.linear1.weight
        assert find_tied_parameters(model) == [["block1.linear1.weight", "block2.linear1.weight"]]

        layer = nn.Linear(10, 10)
        model = nn.Sequential(layer, layer)
        tied_params = find_tied_parameters(model)
        assert sorted(tied_params) == [["0.bias", "1.bias"], ["0.weight", "1.weight"]]

    def test_retie_parameters(self):
        model = sequential_model(2)
        retie_parameters(model, [["linear1.weight", "linear2.weight"]])
        assert model.linear1.weight is model.linear2.weight

        model = sequential_model(3)
        retie_parameters(model, [["linear1.weight", "linear2.weight", "linear3.weight"]])

        assert model.linear1.weight is model.linear2.weight
        assert model.linear1.weight is model.linear3.weight

        model = sequential_model(5)
        retie_parameters(
            model, [["linear1.weight", "linear4.weight"], ["linear2.weight", "linear3.weight", "linear5.weight"]]
        )

        assert model.linear1.weight is model.linear4.weight
        assert model.linear2.weight is model.linear3.weight
        assert model.linear2.weight is model.linear5.weight

        model = nn.Sequential(OrderedDict([("block1", sequential_model(4)), ("block2", sequential_model(4))]))
        retie_parameters(model, [["block1.linear1.weight", "block2.linear1.weight"]])

        assert model.block1.linear1.weight is model.block2.linear1.weight

    def test_compute_module_sizes(self):
        model = ModelForTest()
        expected_sizes = {"": 236, "linear1": 64, "linear1.weight": 48, "linear1.bias": 16}
        expected_sizes.update({"linear2": 100, "linear2.weight": 80, "linear2.bias": 20})
        expected_sizes.update({"batchnorm": 72, "batchnorm.weight": 16, "batchnorm.bias": 16})
        expected_sizes.update(
            {"batchnorm.running_mean": 16, "batchnorm.running_var": 16, "batchnorm.num_batches_tracked": 8}
        )

        module_sizes = compute_module_sizes(model)
        assert module_sizes == expected_sizes

        model.half()
        expected_sizes = {k: s // 2 for k, s in expected_sizes.items()}
        # This one is not converted to half.
        expected_sizes["batchnorm.num_batches_tracked"] = 8
        # This impacts batchnorm and total
        expected_sizes["batchnorm"] += 4
        expected_sizes[""] += 4

        module_sizes = compute_module_sizes(model)
        assert module_sizes == expected_sizes

    def test_compute_module_total_buffer_size(self):
        model = ModelForTest()
        model.linear1.register_buffer("test_buffer", torch.zeros(10, 10))
        model.register_buffer("test_buffer2", torch.zeros(20, 10))

        buffer_size = compute_module_total_buffer_size(model)
        assert buffer_size == 1240

        model.half()
        buffer_size = compute_module_total_buffer_size(model)
        assert buffer_size == 624

    def test_check_device_map(self):
        model = ModelForTest()
        check_device_map(model, {"": 0})
        with self.assertRaises(ValueError):
            check_device_map(model, {"linear1": 0, "linear2": 1})

        check_device_map(model, {"linear1": 0, "linear2": 1, "batchnorm": 1})

    def test_check_device_map_invalid_keys(self):
        model = ModelForTest()

        device_map = {
            "linear1": "cpu",  # Valid module
            "batchnorm": "cpu",  # Valid module
            "linear2": "cpu",  # Valid module
            "invalid_module": 0,  # Invalid - should trigger warning
            "another_invalid": 1,  # Invalid - should trigger warning
        }

        # Test for the warning about invalid keys
        with self.assertWarns(UserWarning) as cm:
            check_device_map(model, device_map)

        warning_msg = str(cm.warning)
        self.assertIn("device_map keys do not match any submodules", warning_msg)
        self.assertIn("invalid_module", warning_msg)
        self.assertIn("another_invalid", warning_msg)

    def shard_test_model(self, model, tmp_dir):
        module_index = {
            "linear1": "checkpoint_part1.bin",
            "batchnorm": "checkpoint_part2.bin",
            "linear2": "checkpoint_part3.bin",
        }
        index = {}
        for name, _ in model.state_dict().items():
            module = name.split(".")[0]
            index[name] = module_index[module]

        with open(os.path.join(tmp_dir, "weight_map.index.json"), "w") as f:
            json.dump(index, f)

        for module, fname in module_index.items():
            state_dict = {k: v for k, v in model.state_dict().items() if k.startswith(module)}
            full_fname = os.path.join(tmp_dir, fname)
            torch.save(state_dict, full_fname)

    def test_load_checkpoint_in_model(self):
        # Check with whole checkpoint
        model = ModelForTest()
        with tempfile.TemporaryDirectory() as tmp_dir:
            fname = os.path.join(tmp_dir, "pt_model.bin")
            torch.save(model.state_dict(), fname)
            load_checkpoint_in_model(model, fname)

        # Check with sharded index
        model = ModelForTest()
        with tempfile.TemporaryDirectory() as tmp_dir:
            self.shard_test_model(model, tmp_dir)
            index_file = os.path.join(tmp_dir, "weight_map.index.json")
            load_checkpoint_in_model(model, index_file)

        # Check with sharded checkpoint
        model = ModelForTest()
        with tempfile.TemporaryDirectory() as tmp_dir:
            self.shard_test_model(model, tmp_dir)
            load_checkpoint_in_model(model, tmp_dir)

    @require_non_cpu
    def test_load_checkpoint_in_model_one_gpu(self):
        device_map = {"linear1": 0, "batchnorm": "cpu", "linear2": "cpu"}

        # Check with whole checkpoint
        model = ModelForTest()
        with tempfile.TemporaryDirectory() as tmp_dir:
            fname = os.path.join(tmp_dir, "pt_model.bin")
            torch.save(model.state_dict(), fname)
            load_checkpoint_in_model(model, fname, device_map=device_map)
        assert model.linear1.weight.device == torch.device(torch_device)
        assert model.batchnorm.weight.device == torch.device("cpu")
        assert model.linear2.weight.device == torch.device("cpu")

        # Check with sharded index
        model = ModelForTest()
        with tempfile.TemporaryDirectory() as tmp_dir:
            self.shard_test_model(model, tmp_dir)
            index_file = os.path.join(tmp_dir, "weight_map.index.json")
            load_checkpoint_in_model(model, index_file, device_map=device_map)

        assert model.linear1.weight.device == torch.device(torch_device)
        assert model.batchnorm.weight.device == torch.device("cpu")
        assert model.linear2.weight.device == torch.device("cpu")

        # Check with sharded checkpoint folder
        model = ModelForTest()
        with tempfile.TemporaryDirectory() as tmp_dir:
            self.shard_test_model(model, tmp_dir)
            load_checkpoint_in_model(model, tmp_dir, device_map=device_map)

        assert model.linear1.weight.device == torch.device(torch_device)
        assert model.batchnorm.weight.device == torch.device("cpu")
        assert model.linear2.weight.device == torch.device("cpu")

    @require_non_cpu
    def test_load_checkpoint_in_model_disk_offload(self):
        device_map = {"linear1": "cpu", "batchnorm": "disk", "linear2": "cpu"}

        model = ModelForTest()
        with tempfile.TemporaryDirectory() as tmp_dir:
            fname = os.path.join(tmp_dir, "pt_model.bin")
            torch.save(model.state_dict(), fname)
            load_checkpoint_in_model(model, fname, device_map=device_map, offload_folder=tmp_dir)
        assert model.linear1.weight.device == torch.device("cpu")
        assert model.batchnorm.weight.device == torch.device("meta")
        # Buffers are not offloaded by default
        assert model.batchnorm.running_mean.device == torch.device("cpu")
        assert model.linear2.weight.device == torch.device("cpu")

        model = ModelForTest()
        with tempfile.TemporaryDirectory() as tmp_dir:
            fname = os.path.join(tmp_dir, "pt_model.bin")
            torch.save(model.state_dict(), fname)
            load_checkpoint_in_model(model, fname, device_map=device_map, offload_folder=tmp_dir, offload_buffers=True)
        assert model.linear1.weight.device == torch.device("cpu")
        assert model.batchnorm.weight.device == torch.device("meta")
        assert model.batchnorm.running_mean.device == torch.device("meta")
        assert model.linear2.weight.device == torch.device("cpu")

    @require_non_hpu  # hpu does not support device indexing "hpu:1"
    @require_multi_device
    def test_load_checkpoint_in_model_two_gpu(self):
        device_map = {"linear1": 0, "batchnorm": "cpu", "linear2": 1}

        # Check with whole checkpoint
        model = ModelForTest()
        with tempfile.TemporaryDirectory() as tmp_dir:
            fname = os.path.join(tmp_dir, "pt_model.bin")
            torch.save(model.state_dict(), fname)
            load_checkpoint_in_model(model, fname, device_map=device_map)
        assert model.linear1.weight.device == torch.device(torch_device)
        assert model.batchnorm.weight.device == torch.device("cpu")
        assert model.linear2.weight.device == torch.device(torch_device.replace("0", "1"))

        # Check with sharded index
        model = ModelForTest()
        with tempfile.TemporaryDirectory() as tmp_dir:
            self.shard_test_model(model, tmp_dir)
            index_file = os.path.join(tmp_dir, "weight_map.index.json")
            load_checkpoint_in_model(model, index_file, device_map=device_map)

        assert model.linear1.weight.device == torch.device(torch_device)
        assert model.batchnorm.weight.device == torch.device("cpu")
        assert model.linear2.weight.device == torch.device(torch_device.replace("0", "1"))

        # Check with sharded checkpoint
        model = ModelForTest()
        with tempfile.TemporaryDirectory() as tmp_dir:
            self.shard_test_model(model, tmp_dir)
            load_checkpoint_in_model(model, tmp_dir, device_map=device_map)

        assert model.linear1.weight.device == torch.device(torch_device)
        assert model.batchnorm.weight.device == torch.device("cpu")
        assert model.linear2.weight.device == torch.device(torch_device.replace("0", "1"))

    def test_load_checkpoint_in_model_dtype(self):
        with tempfile.NamedTemporaryFile(suffix=".pt") as tmpfile:
            model = ModelSeveralDtypes()
            torch.save(model.state_dict(), tmpfile.name)

            new_model = ModelSeveralDtypes()
            load_checkpoint_in_model(
                new_model, tmpfile.name, offload_state_dict=True, dtype=torch.float16, device_map={"": "cpu"}
            )

            assert new_model.int_param.dtype == torch.int64
            assert new_model.float_param.dtype == torch.float16

    @parameterized.expand([(None,), ({"": "cpu"},)])
    def test_load_checkpoint_in_model_unexpected_keys(self, device_map: Optional[dict]):
        model = ModelForTest()

        state_dict = model.state_dict()
        state_dict["foo"] = torch.rand(4, 5)
        with tempfile.NamedTemporaryFile(suffix=".pt") as tmpfile:
            torch.save(state_dict, tmpfile)

            model = ModelForTest()

            with self.assertLogs() as cm:
                load_checkpoint_in_model(model, tmpfile.name, device_map=device_map)

                self.assertTrue(any("were not used when" in out for out in cm.output))

            with self.assertRaises((ValueError, RuntimeError)):
                load_checkpoint_in_model(model, tmpfile.name, device_map=device_map, strict=True)

    def test_clean_device_map(self):
        # Regroup everything if all is on the same device
        assert clean_device_map({"a": 0, "b": 0, "c": 0}) == {"": 0}
        # Regroups children of level 1 on the same device
        assert clean_device_map({"a.x": 0, "a.y": 0, "b.x": 1, "b.y": 1, "c": 1}) == {"a": 0, "b": 1, "c": 1}
        # Regroups children of level 2 on the same device
        assert clean_device_map({"a.x": 0, "a.y": 0, "b.x.0": 1, "b.x.1": 1, "b.y.0": 2, "b.y.1": 2, "c": 2}) == {
            "a": 0,
            "b.x": 1,
            "b.y": 2,
            "c": 2,
        }

    def test_infer_auto_device_map(self):
        model = ModelForTest()
        # model has size 236: linear1 64, batchnorm 72, linear2 100
        try:
            with self.assertLogs() as cm:
                device_map = infer_auto_device_map(model, max_memory={0: 200, 1: 200})
                self.assertFalse(any("insufficient memory" in out for out in cm.output))
        except AssertionError:
            # No logs exist; test passes implicitly
            pass

        # only linear1 fits on device 0 as we keep memory available for the maximum layer in case of offload
        assert device_map == {"linear1": 0, "batchnorm": 1, "linear2": 1}

        device_map = infer_auto_device_map(model, max_memory={0: 200, 1: 172, 2: 200})
        # On device 1, we don't care about keeping size available for the max layer, so even if there is just the
        # size available for batchnorm + linear2, they fit here.
        assert device_map == {"linear1": 0, "batchnorm": 1, "linear2": 1}

        model.linear1.weight = model.linear2.weight
        device_map = infer_auto_device_map(model, max_memory={0: 200, 1: 200})
        # By tying weights, the whole model fits on device 0
        assert device_map == {"": 0}

        # When splitting a bigger model, the split is done at the layer level
        model = nn.Sequential(ModelForTest(), ModelForTest(), ModelForTest())
        device_map = infer_auto_device_map(model, max_memory={0: 500, 1: 500})
        assert device_map == {"0": 0, "1.linear1": 0, "1.batchnorm": 0, "1.linear2": 1, "2": 1}

        # With no_split_module_classes, it's done at that module level
        model = nn.Sequential(ModelForTest(), ModelForTest(), ModelForTest())
        device_map = infer_auto_device_map(
            model, max_memory={0: 500, 1: 500}, no_split_module_classes=["ModelForTest"]
        )
        assert device_map == {"0": 0, "1": 1, "2": 1}

    def test_infer_auto_device_map_with_tied_weights(self):
        model = nn.Sequential(
            OrderedDict([("layer1", ModelForTest()), ("layer2", ModelForTest()), ("layer3", ModelForTest())])
        )
        model.layer3.linear2.weight = model.layer1.linear2.weight
        device_map = infer_auto_device_map(model, max_memory={0: 400, 1: 500})
        expected = {"layer1": 0, "layer3.linear2": 0, "layer2": 1, "layer3.linear1": 1, "layer3.batchnorm": 1}
        assert device_map == expected

        # With three weights tied together
        model.layer2.linear2.weight = model.layer1.linear2.weight
        device_map = infer_auto_device_map(model, max_memory={0: 400, 1: 500})
        expected = {
            "layer1": 0,
            "layer2.linear2": 0,
            "layer3.linear2": 0,
            "layer2.linear1": 1,
            "layer2.batchnorm": 1,
            "layer3.linear1": 1,
            "layer3.batchnorm": 1,
        }
        assert device_map == expected

        # With two groups of weights tied together
        model.layer2.linear1.weight = model.layer1.linear1.weight
        device_map = infer_auto_device_map(model, max_memory={0: 400, 1: 500})
        expected = {
            "layer1": 0,
            "layer2.linear1": 0,
            "layer2.linear2": 0,
            "layer3.linear2": 0,
            "layer2.batchnorm": 1,
            "layer3.linear1": 1,
            "layer3.batchnorm": 1,
        }
        assert device_map == expected

        # With weights ties in the same module
        model = nn.Sequential(
            OrderedDict(
                [
                    ("linear1", nn.Linear(4, 4)),
                    ("linear2", nn.Linear(6, 6)),
                    ("linear3", nn.Linear(4, 4)),
                    ("linear4", nn.Linear(6, 6)),
                ]
            )
        )
        model.linear3.weight = model.linear1.weight
        model.linear3.bias = model.linear1.bias
        device_map = infer_auto_device_map(model, max_memory={0: 250, 1: 400})
        expected = {"linear1": 0, "linear2": 1, "linear3": 0, "linear4": 1}
        assert device_map == expected

        # With tied weights sharing a same prefix name (`compute.weight` vs `compute.weight_submodule.parameter`)
        class SubModule(torch.nn.Module):
            def __init__(self, ref_to_parameter):
                super().__init__()
                self.parameter = ref_to_parameter

            def forward(self, x):
                return self.x + torch.max(self.parameter)

        class LinearModuleAndSubModule(torch.nn.Linear):
            def __init__(self, in_features, out_features):
                super().__init__(in_features, out_features)
                self.weight_submodule = SubModule(self.weight)

            def forward(self, x):
                return torch.nn.functional.linear(self.weight_submodule(x), self.weight)

        class Model(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.compute = LinearModuleAndSubModule(3, 8)

            def forward(self, x):
                return self.compute(x)

        model = Model()

        device_memory = {0: 4, "cpu": 96000}  # Low memory device, just to force splitting and trigger the error
        infer_auto_device_map(model, device_memory)

    @require_huggingface_suite
    def test_infer_auto_device_map_on_t0pp(self):
        from transformers import AutoConfig, AutoModelForSeq2SeqLM

        config = AutoConfig.from_pretrained("bigscience/T0pp")
        with init_empty_weights():
            model = AutoModelForSeq2SeqLM.from_config(config)
        model.tie_weights()

        special_dtypes = {n: torch.float32 for n, _ in model.named_parameters() if "wo" in n}
        max_memory = {0: 10**10, 1: 10**10, "cpu": 10**10}
        device_map = infer_auto_device_map(
            model,
            no_split_module_classes=["T5Block"],
            dtype=torch.float16,
            max_memory=max_memory,
            special_dtypes=special_dtypes,
        )

        # The 3 tied weights should all be on device 0
        assert device_map["shared"] == 0
        assert device_map["encoder.embed_tokens"] == 0
        assert device_map["decoder.embed_tokens"] == 0

    def test_infer_auto_device_map_with_buffer_check(self):
        model = ModelForTest()
        model.linear1.register_buffer("test_buffer1", torch.zeros(10, 2))
        model.batchnorm.register_buffer("test_buffer2", torch.zeros(10, 3))
        model.linear2.register_buffer("test_buffer3", torch.zeros(10, 3))
        # model has size 236(parameters) + 360(buffers): linear1 64 + 80, batchnorm 72 + 160, linear2 100 + 120

        # Only linear1 (144) fits on device 0, and remaining buffers (batchnorm's 160 + linear2's 120 = 280) won't fit
        # device 0, because they will also be loaded to device 0 all at once when inferencing without offload_buffers
        # Should print a warning as intended in such case
        with self.assertWarns(Warning):
            device_map = infer_auto_device_map(model, max_memory={0: 400, "cpu": "1GB"})
        assert device_map == {"linear1": 0, "batchnorm": "cpu", "linear2": "cpu"}

        # Only linear1 (144) fits on device 0, and remaining buffers (batchnorm's 160 + linear2's 120 = 280) won't fit
        # device 0, but with offload_buffers they won't be loaded to device 0 all at once, so it's ok now
        # Should NOT print a warning in such case
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            device_map = infer_auto_device_map(model, max_memory={0: 400, "cpu": "1GB"}, offload_buffers=True)
        assert len(w) == 0
        assert device_map == {"linear1": 0, "batchnorm": "cpu", "linear2": "cpu"}

    def test_infer_auto_device_map_with_buffer_check_and_multi_devices(self):
        model = ModelForTest()
        model.linear1.register_buffer("test_buffer1", torch.zeros(10, 2))
        model.batchnorm.register_buffer("test_buffer2", torch.zeros(10, 3))
        model.linear2.register_buffer("test_buffer3", torch.zeros(10, 3))
        model.linear3 = nn.Linear(4, 5)
        model.linear3.register_buffer("test_buffer4", torch.zeros(10, 2))
        # model has size 336(parameters) + 440(buffers): linear1 64 + 80, batchnorm 72 + 160, linear2 100 + 120,
        # linear3 100 + 80

        # Now we have two devices, linear1 will fit on device 0, batchnorm will fit on device 1, and the second device
        # can hold all remaining buffers
        # Should NOT print a warning in such case
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            device_map = infer_auto_device_map(model, max_memory={0: 400, 1: 400, "cpu": "1GB"})
        assert len(w) == 0
        assert device_map == {"linear1": 0, "batchnorm": 1, "linear2": "cpu", "linear3": "cpu"}

        # Now we have two devices, but neither the first nor the second device can hold all remaining buffers
        # Should print a warning as intended in such case
        with self.assertWarns(Warning):
            device_map = infer_auto_device_map(model, max_memory={0: 400, 1: 200, "cpu": "1GB"})
        assert device_map == {"linear1": 0, "batchnorm": 1, "linear2": "cpu", "linear3": "cpu"}

        # Now we have two devices, neither can hold all the buffers, but we are using the offload_buffers=True
        # Should NOT print a warning in such case
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            device_map = infer_auto_device_map(model, max_memory={0: 400, 1: 200, "cpu": "1GB"}, offload_buffers=True)
        assert len(w) == 0
        assert device_map == {"linear1": 0, "batchnorm": 1, "linear2": "cpu", "linear3": "cpu"}

    def test_infer_auto_device_map_with_fallback_allocation(self):
        # Create a model where modules cannot be allocated without fallback_allocation
        # Define the inner module with its layers
        inner_module = nn.Sequential(
            OrderedDict([("linear1", nn.Linear(10, 4)), ("linear2", nn.Linear(4, 4)), ("linear3", nn.Linear(4, 8))])
        )

        # Wrap the inner module in another module
        model = nn.Sequential(OrderedDict([("module", inner_module)]))

        max_memory = {0: 256}

        # Without fallback_allocation
        with self.assertLogs() as cm:
            device_map = infer_auto_device_map(model, max_memory=max_memory, fallback_allocation=False)
            # No module should be assigned to device 0
            assert all(device != 0 for device in device_map.values())
            # Check for warning about insufficient memory
            self.assertTrue(any("insufficient memory" in out for out in cm.output))

        # With fallback_allocation
        try:
            with self.assertLogs() as cm:
                device_map = infer_auto_device_map(model, max_memory=max_memory, fallback_allocation=True)
                self.assertFalse(any("insufficient memory" in out for out in cm.output))
        except AssertionError:
            # No logs exist; test passes implicitly
            pass
        # At least one submodule should be assigned to device 0
        assert any(device == 0 for device in device_map.values())

        expected_device_map = {"module.linear1": "disk", "module.linear2": 0, "module.linear3": "disk"}
        assert device_map == expected_device_map

    def test_infer_auto_device_map_with_fallback_allocation_no_fit(self):
        # Create a model where even the smallest submodules cannot fit
        inner_module = nn.Sequential(
            OrderedDict(
                [("linear1", nn.Linear(10, 10)), ("linear2", nn.Linear(10, 10)), ("linear3", nn.Linear(10, 10))]
            )
        )

        # Wrap the inner module in another module
        model = nn.Sequential(OrderedDict([("module", inner_module)]))

        max_memory = {0: 30}

        # With fallback_allocation
        try:
            with self.assertLogs() as cm:
                device_map = infer_auto_device_map(model, max_memory=max_memory, fallback_allocation=True)
                # No module should be assigned to device 0
                assert all(device != 0 for device in device_map.values())
                # Check for warning about insufficient memory
                self.assertTrue(any("insufficient memory" in out for out in cm.output))
        except AssertionError:
            # No logs exist; test passes implicitly
            pass

    def test_infer_auto_device_map_with_fallback_allocation_partial_fit(self):
        # Create a model with deeper hierarchy
        class CustomModule(nn.Module):
            def __init__(self):
                super().__init__()
                self.submodule1 = nn.Linear(20, 20)
                self.submodule2 = nn.Linear(20, 20)

        model = nn.Sequential(
            OrderedDict([("module1", CustomModule()), ("module2", CustomModule()), ("module3", CustomModule())])
        )

        max_memory = {0: 5000}

        # With fallback_allocation
        device_map = infer_auto_device_map(model, max_memory=max_memory, fallback_allocation=True)
        # Check that at least some parameters are assigned to device 0
        assigned_to_device_0 = [name for name, device in device_map.items() if device == 0]
        assert len(assigned_to_device_0) > 0

    def test_infer_auto_device_map_with_fallback_allocation_tied_weights(self):
        # Create a model with tied weights
        class TiedWeightsModel(nn.Module):
            def __init__(self):
                super().__init__()
                self.linear1 = nn.Linear(10, 10)
                self.linear2 = nn.Linear(10, 10)
                self.linear2.weight = self.linear1.weight

        model = TiedWeightsModel()

        max_memory = {0: 600}

        # With fallback_allocation
        device_map = infer_auto_device_map(model, max_memory=max_memory, fallback_allocation=True)
        # Check that tied modules are assigned correctly
        expected_device_map = {"": 0}
        assert device_map == expected_device_map

    def test_infer_auto_device_map_with_fallback_allocation_and_buffers(self):
        # Create a model with buffers
        model = nn.Sequential(
            OrderedDict(
                [("linear1", nn.Linear(10, 10)), ("batchnorm", nn.BatchNorm1d(10)), ("linear2", nn.Linear(10, 10))]
            )
        )
        model.linear1.register_buffer("buffer1", torch.zeros(5))
        model.batchnorm.register_buffer("buffer2", torch.zeros(5))
        model.linear2.register_buffer("buffer3", torch.zeros(5))

        max_memory = {0: 678}

        # With fallback_allocation and offload_buffers=False
        with self.assertWarns(Warning) as cm:
            device_map = infer_auto_device_map(
                model, max_memory=max_memory, fallback_allocation=True, offload_buffers=False
            )

        # Check that the warning contains the expected message
        warning_message = str(cm.warning)
        assert "offload_buffers" in warning_message or "Current model requires" in warning_message

        # Verify that the entire model is assigned to device 0
        expected_device_map = {"batchnorm": 0, "linear1": "disk", "linear2": "disk"}
        assert device_map == expected_device_map

    @require_non_cpu
    def test_get_balanced_memory(self):
        model = ModelForTest()
        # model has size 236: linear1 64, batchnorm 72, linear2 100
        max_memory = get_balanced_memory(model, max_memory={0: 200, 1: 200})
        assert {0: 200, 1: 200} == max_memory

        # We should be able to set models on a non-contiguous sub-set of
        max_memory = get_balanced_memory(model, max_memory={0: 200, 2: 200})
        assert {0: 200, 2: 200} == max_memory

        max_memory = get_balanced_memory(model, max_memory={0: 300, 1: 300})
        assert {0: 215, 1: 300} == max_memory

        # Last device always get max memory to give more buffer and avoid accidental CPU offload
        max_memory = get_balanced_memory(model, max_memory={0: 300, 1: 500})
        assert {0: 215, 1: 500} == max_memory

        # Last device always get max memory to give more buffer, even if CPU is provided
        max_memory = get_balanced_memory(model, max_memory={0: 300, "cpu": 1000})
        assert {0: 300, "cpu": 1000} == max_memory

        # If we set a device to 0, it's not counted.
        max_memory = get_balanced_memory(model, max_memory={0: 0, 1: 300, 2: 300})
        assert {0: 0, 1: 215, 2: 300} == max_memory

        # If we set a device to 0, it's not counted.
        max_memory = get_balanced_memory(model, max_memory={0: 0, "cpu": 100})
        assert {0: 0, "cpu": 100} == max_memory

    # Tests that get_module_size_with_ties returns the correct tied modules in
    # models with tied parameters whose parent modules share the same name prefix
    # See issue #3308: https://github.com/huggingface/accelerate/issues/3308
    def test_get_module_size_with_ties(self):
        # Create a model with a ModuleList containing more than 10 elements
        # so the names of some layers share the same prefix, e.g. "1" and "10"
        num_layers = 15
        model = nn.ModuleList([nn.Linear(10, 10) for _ in range(num_layers)])
        # Tie .weight for all the layers
        for i in range(1, num_layers):
            model[i].weight = model[i - 1].weight
        # Each tied parameter group is sorted in alphabetical ordering,
        # mimicking the output of find_tied_parameters
        tied_parameters = [sorted([f"{i}.weight" for i in range(num_layers)])]
        # Compute module sizes
        weight_size, bias_size = (
            model[0].weight.element_size() * model[0].weight.numel(),
            model[0].bias.element_size() * model[0].bias.numel(),
        )
        module_sizes = dict(
            **{"": num_layers * (weight_size + bias_size)},
            **{f"{i}": (weight_size + bias_size) for i in range(num_layers)},
            **{f"{i}.weight": weight_size for i in range(num_layers)},
            **{f"{i}.bias": bias_size for i in range(num_layers)},
        )
        # Simulate the input for get_module_size_with_ties when invoked from infer_auto_device_map
        # when the first module in model is being processed
        modules_to_treat = list(model.named_children())[1:]
        tied_params = tied_parameters[0][1:]
        module_size = weight_size + bias_size

        module_size_with_ties, tied_module_names, tied_modules = get_module_size_with_ties(
            tied_params, module_size, module_sizes, modules_to_treat
        )
        # The expected lists are ordered using as key the module names, to follow
        # the same order as the tied_parameters returned by find_tied_parameters
        expected_tied_module_names, expected_tied_modules = map(
            list, zip(*sorted(modules_to_treat, key=lambda x: x[0]))
        )

        assert module_size_with_ties == module_size + (num_layers - 1) * bias_size
        assert tied_module_names == expected_tied_module_names
        assert tied_modules == expected_tied_modules

    @require_non_cpu
    def test_load_state_dict(self):
        state_dict = {k: torch.randn(4, 5) for k in ["a", "b", "c"]}
        device_maps = [{"a": "cpu", "b": 0, "c": "disk"}, {"a": 0, "b": 0, "c": "disk"}, {"a": 0, "b": 0, "c": 0}]

        for device_map in device_maps:
            with tempfile.TemporaryDirectory() as tmp_dir:
                checkpoint_file = os.path.join(tmp_dir, "model.safetensors")
                save_file(state_dict, checkpoint_file, metadata={"format": "pt"})

                loaded_state_dict = load_state_dict(checkpoint_file, device_map=device_map)

            for param, device in device_map.items():
                device = device if device != "disk" else "cpu"
                assert loaded_state_dict[param].device == torch.device(device)

    def test_convert_file_size(self):
        result = convert_file_size_to_int("0MB")
        assert result == 0

        result = convert_file_size_to_int("100MB")
        assert result == (100 * (10**6))

        result = convert_file_size_to_int("2GiB")
        assert result == (2 * (2**30))

        result = convert_file_size_to_int("512KiB")
        assert result == (512 * (2**10))

        result = convert_file_size_to_int("1.5GB")
        assert result == (1.5 * (10**9))

        result = convert_file_size_to_int("100KB")
        assert result == (100 * (10**3))

        result = convert_file_size_to_int(500)
        assert result == 500

        with self.assertRaises(ValueError):
            convert_file_size_to_int("5MBB")

        with self.assertRaises(ValueError):
            convert_file_size_to_int("5k0MB")

        with self.assertRaises(ValueError):
            convert_file_size_to_int("-1GB")

    def test_get_state_dict_offloaded_model(self):
        for model_cls in (ModelForTest, NestedModelForTest):
            model = model_cls()
            execution_device = torch.device(torch_device)
            original_state_dict = model.state_dict()

            cpu_offload(model, execution_device=execution_device)
            state_dict = get_state_dict_offloaded_model(model)

            assert original_state_dict.keys() == state_dict.keys()
            for key in original_state_dict:
                assert torch.equal(original_state_dict[key], state_dict[key])

    def test_align_module_device_simple(self):
        model = ModelForTest()
        execution_device = torch.device(torch_device)
        model_device = torch.device("cpu")

        # test default execution device
        with align_module_device(model.batchnorm):
            assert model.linear1.weight.device == model_device
            assert model.batchnorm.weight.device == model_device
            assert model.linear2.weight.device == model_device
        assert model.linear1.weight.device == model_device
        assert model.batchnorm.weight.device == model_device
        assert model.linear2.weight.device == model_device

        # test with explicit execution device
        with align_module_device(model.batchnorm, execution_device=execution_device):
            assert model.linear1.weight.device == model_device
            assert model.batchnorm.weight.device == execution_device
            assert model.linear2.weight.device == model_device
        assert model.linear1.weight.device == model_device
        assert model.batchnorm.weight.device == model_device
        assert model.linear2.weight.device == model_device

    def test_align_module_device_offloaded(self):
        model = ModelForTest()
        execution_device = torch.device(torch_device)
        offload_device = torch.device("meta")
        cpu_offload(model, execution_device=execution_device)

        # test default execution device
        with align_module_device(model.batchnorm):
            assert model.linear1.weight.device == offload_device
            assert model.batchnorm.weight.device == execution_device
            assert model.linear2.weight.device == offload_device
        assert model.linear1.weight.device == offload_device
        assert model.batchnorm.weight.device == offload_device
        assert model.linear2.weight.device == offload_device

        # test with explicit execution device
        with align_module_device(model.batchnorm, execution_device="cpu"):
            assert model.linear1.weight.device == offload_device
            assert model.batchnorm.weight.device == torch.device("cpu")
            assert model.linear2.weight.device == offload_device
        assert model.linear1.weight.device == offload_device
        assert model.batchnorm.weight.device == offload_device
        assert model.linear2.weight.device == offload_device

    def test_align_module_device_offloaded_nested(self):
        model = NestedModelForTest()
        execution_device = torch.device(torch_device)
        align_device = torch.device("cpu")
        cpu_offload(model, execution_device=execution_device)
        for module in model.modules():
            with align_module_device(module, align_device):
                for param in model.parameters(recurse=False):
                    assert param.device == align_device
