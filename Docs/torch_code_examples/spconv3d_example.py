# Step 0. Import statements
import torch
import spconv.pytorch as spconv
import aimet_torch
from aimet_torch.quantsim import QuantizationSimModel, QuantScheme
from aimet_torch.pro.model_preparer import prepare_model
# End step 0

import tempfile

# Step 1. Create or load model with SpConv3D module(s)
class SpConvModel(torch.nn.Module):
    def __init__(self):
        super(SpConvModel, self).__init__()
        self.spconv_tensor = aimet_torch.nn.modules.custom.SparseTensorWrapper()
        self.spconv1 = spconv.SparseConv3d(in_channels=3, out_channels=9, kernel_size=2,
                                           bias=False)
        self.spconv2 = spconv.SparseConv3d(in_channels=9, out_channels=5, kernel_size=3, bias=False)
        self.normal_conv3d = torch.nn.Conv3d(in_channels=5, out_channels=3, kernel_size=3, bias=True)
        self.spconv_scatter_dense = aimet_torch.nn.modules.custom.ScatterDense()
        self.relu1 = torch.nn.ReLU()

    def forward(self, coords, voxels):
        sp_tensor = self.spconv_tensor(coords, voxels)
        sp_outputs1 = self.spconv1(sp_tensor)
        sp_outputs2 = self.spconv2(sp_outputs1)
        sp_outputs2_dense = self.spconv_scatter_dense(sp_outputs2)
        sp_outputs = self.normal_conv3d(sp_outputs2_dense)
        sp_outputs_relu = self.relu1(sp_outputs)
        return sp_outputs_relu
# End Step 1

model = SpConvModel()

# Step 2. Obtain model inputs
dense_tensor_sp_inputs = torch.randn(1, 3, 10, 10, 10) # generate a random NCDHW tensor
dense_tensor_sp_inputs = dense_tensor_sp_inputs.permute(0, 2, 3, 4, 1) # convert NCDHW to NDHWC
indices = torch.stack(torch.meshgrid(torch.arange(dense_tensor_sp_inputs.shape[0]), torch.arange(dense_tensor_sp_inputs.shape[1]),
                                     torch.arange(dense_tensor_sp_inputs.shape[2]), torch.arange(dense_tensor_sp_inputs.shape[3]),
                                     indexing='ij'), dim=-1).reshape(-1, 4).int()
features = dense_tensor_sp_inputs.view(-1, dense_tensor_sp_inputs.shape[4])
# End Step 2

with torch.no_grad():
    orig_output = model(indices, features)

with tempfile.TemporaryDirectory() as dir:
    # Step 3. Apply model preparer pro
    prepared_model = prepare_model(model, dummy_input=(indices, features), path=dir,
                                   onnx_export_args=dict(operator_export_type=
                                                         torch.onnx.OperatorExportTypes.ONNX_ATEN_FALLBACK,
                                                         opset_version=16),
                                   converter_args=['--input_dtype', "indices.1", "int32", '--input_dtype',
                                                   "features.1", "float32", '--expand_sparse_op_structure',
                                                   '--preserve_io', 'datatype', 'indices.1'])
    # End Step 3

with torch.no_grad():
    prep_output = prepared_model(indices, features)

# Step 4. Apply QuantSim
qsim = QuantizationSimModel(prepared_model, dummy_input=(indices, features),
                            quant_scheme=QuantScheme.post_training_tf)
# End Step 4

def dummy_forward_pass(model, inp):
    with torch.no_grad():
        _ = model(*inp)

# Step 5. Compute encodings
qsim.compute_encodings(dummy_forward_pass, (indices, features))
# End Step 5

with torch.no_grad():
    qsim_output = qsim.model(indices, features)

with tempfile.TemporaryDirectory() as dir:
    # Step 6. QuantSim export
    qsim.export(dir, "exported_sp_conv_model", dummy_input=(indices, features),
                onnx_export_args=dict(operator_export_type=torch.onnx.OperatorExportTypes.ONNX_ATEN_FALLBACK))
    # End Step 6
