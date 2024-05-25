from typing import Any

import einops
import pytest
import torch
from datasets import Dataset
from transformer_lens import HookedTransformer

from sae_lens.training.activations_store import ActivationsStore
from sae_lens.training.config import LanguageModelSAERunnerConfig
from sae_lens.training.sae_trainer import SAETrainer
from sae_lens.training.sparse_autoencoder import TrainingSparseAutoencoder
from tests.unit.helpers import build_sae_cfg


# Define a new fixture for different configurations
@pytest.fixture(
    params=[
        {
            "model_name": "tiny-stories-1M",
            "dataset_path": "roneneldan/TinyStories",
            "tokenized": False,
            "hook_point": "blocks.1.hook_resid_pre",
            "hook_point_layer": 1,
            "d_in": 64,
        },
        {
            "model_name": "tiny-stories-1M",
            "dataset_path": "roneneldan/TinyStories",
            "tokenized": False,
            "hook_point": "blocks.1.hook_resid_pre",
            "hook_point_layer": 1,
            "d_in": 64,
            "normalize_sae_decoder": False,
            "scale_sparsity_penalty_by_decoder_norm": True,
        },
        {
            "model_name": "tiny-stories-1M",
            "dataset_path": "apollo-research/roneneldan-TinyStories-tokenizer-gpt2",
            "tokenized": False,
            "hook_point": "blocks.1.hook_resid_pre",
            "hook_point_layer": 1,
            "d_in": 64,
        },
        {
            "model_name": "tiny-stories-1M",
            "dataset_path": "roneneldan/TinyStories",
            "tokenized": False,
            "hook_point": "blocks.1.attn.hook_z",
            "hook_point_layer": 1,
            "d_in": 2048,
        },
    ],
    ids=[
        "tiny-stories-1M-resid-pre",
        "tiny-stories-1M-resid-pre-L1-W-dec-Norm",
        "tiny-stories-1M-resid-pre-pretokenized",
        "tiny-stories-1M-hook-z",
    ],
)
def cfg(request: pytest.FixtureRequest):
    """
    Pytest fixture to create a mock instance of LanguageModelSAERunnerConfig.
    """
    params = request.param
    return build_sae_cfg(**params)


@pytest.fixture
def training_sae(cfg: Any):
    """
    Pytest fixture to create a mock instance of SparseAutoencoder.
    """
    return TrainingSparseAutoencoder(cfg)


@pytest.fixture
def activation_store(model: HookedTransformer, cfg: LanguageModelSAERunnerConfig):
    return ActivationsStore.from_config(
        model, cfg, dataset=Dataset.from_list([{"text": "hello world"}] * 2000)
    )


@pytest.fixture
def model(cfg: LanguageModelSAERunnerConfig):
    return HookedTransformer.from_pretrained(cfg.model_name, device="cpu")


@pytest.fixture
def trainer(
    cfg: LanguageModelSAERunnerConfig,
    training_sae: TrainingSparseAutoencoder,
    model: HookedTransformer,
    activation_store: ActivationsStore,
):

    trainer = SAETrainer(
        model=model,
        sae=training_sae,
        activation_store=activation_store,
        save_checkpoint_fn=lambda *args, **kwargs: None,
        cfg=cfg,
    )

    return trainer


# TODO: DECIDE IF WE ARE KEEPING ENCODE AND DECODE METHODS

# def test_sparse_autoencoder_encode(training_sae: TrainingSparseAutoencoder):
#     batch_size = 32
#     d_in = training_sae.d_in
#     d_sae = training_sae.d_sae

#     x = torch.randn(batch_size, d_in)
#     feature_acts1 = training_sae.encode(x)
#     _, cache = training_sae.run_with_cache(x, names_filter="hook_sae_acts_post")
#     feature_acts2 = cache["hook_sae_acts_post"]

#     # Check shape
#     assert feature_acts2.shape == (batch_size, d_sae)

#     # Check values
#     assert torch.allclose(feature_acts1, feature_acts2)


# def test_sparse_autoencoder_decode(training_sae: TrainingSparseAutoencoder):
#     batch_size = 32
#     d_in = training_sae.d_in

#     x = torch.randn(batch_size, d_in)
#     sae_out1 = training_sae(x)

#     assert sae_out1.shape == x.shape
#     assert torch.allclose(sae_out1, sae_out2)


def test_sparse_autoencoder_forward(trainer: SAETrainer):
    batch_size = 32
    d_in = trainer.cfg.d_in
    d_sae = trainer.cfg.d_sae

    x = torch.randn(batch_size, d_in)
    train_step_output = trainer._training_forward_pass(
        sae_in=x,
    )

    assert train_step_output.sae_out.shape == (batch_size, d_in)
    assert train_step_output.feature_acts.shape == (batch_size, d_sae)
    assert pytest.approx(train_step_output.loss.detach(), rel=1e-3) == (
        train_step_output.mse_loss
        + train_step_output.l1_loss
        + train_step_output.ghost_grad_loss
    )

    expected_mse_loss = (
        (torch.pow((train_step_output.sae_out - x.float()), 2))
        .sum(dim=-1)
        .mean()
        .detach()
        .float()
    )

    assert pytest.approx(train_step_output.mse_loss) == expected_mse_loss

    if not trainer.cfg.scale_sparsity_penalty_by_decoder_norm:
        expected_l1_loss = train_step_output.feature_acts.sum(dim=1).mean(dim=(0,))
    else:
        expected_l1_loss = (
            (train_step_output.feature_acts * trainer.sae.W_dec.norm(dim=1))
            .norm(dim=1, p=1)
            .mean()
        )
    assert (
        pytest.approx(train_step_output.l1_loss, rel=1e-3)
        == trainer.cfg.l1_coefficient * expected_l1_loss.detach().float()
    )


def test_sparse_autoencoder_forward_with_mse_loss_norm(
    trainer: SAETrainer,
):
    # change the confgi and ensure the mse loss is calculated correctly
    trainer.cfg.mse_loss_normalization = "dense_batch"
    trainer.mse_loss_fn = trainer._get_mse_loss_fn()

    batch_size = 32
    d_in = trainer.cfg.d_in
    d_sae = trainer.cfg.d_sae

    x = torch.randn(batch_size, d_in)
    train_step_output = trainer._training_forward_pass(
        sae_in=x,
    )

    assert train_step_output.sae_out.shape == (batch_size, d_in)
    assert train_step_output.feature_acts.shape == (batch_size, d_sae)
    assert train_step_output.ghost_grad_loss == 0.0

    x_centred = x - x.mean(dim=0, keepdim=True)
    expected_mse_loss = (
        (
            torch.nn.functional.mse_loss(train_step_output.sae_out, x, reduction="none")
            / (1e-6 + x_centred.norm(dim=-1, keepdim=True))
        )
        .sum(dim=-1)
        .mean()
        .detach()
        .item()
    )

    assert pytest.approx(train_step_output.mse_loss) == expected_mse_loss

    assert pytest.approx(train_step_output.loss.detach(), rel=1e-3) == (
        train_step_output.mse_loss
        + train_step_output.l1_loss
        + train_step_output.ghost_grad_loss
    )

    if not trainer.cfg.scale_sparsity_penalty_by_decoder_norm:
        expected_l1_loss = train_step_output.feature_acts.sum(dim=1).mean(dim=(0,))
    else:
        expected_l1_loss = (
            (train_step_output.feature_acts * trainer.sae.W_dec.norm(dim=1))
            .norm(dim=1, p=1)
            .mean()
        )
    assert (
        pytest.approx(train_step_output.l1_loss, rel=1e-3)
        == trainer.cfg.l1_coefficient * expected_l1_loss.detach().float()
    )


def test_SparseAutoencoder_forward_ghost_grad_loss_non_zero(
    trainer: SAETrainer,
):

    trainer.cfg.use_ghost_grads = True
    batch_size = 32
    d_in = trainer.cfg.d_in
    x = torch.randn(batch_size, d_in)
    train_step_output = trainer._training_forward_pass(
        sae_in=x,
    )

    assert train_step_output.ghost_grad_loss != 0.0


def test_calculate_ghost_grad_loss(
    trainer: SAETrainer,
):
    trainer.cfg.use_ghost_grads = True
    batch_size = 32
    d_in = trainer.cfg.d_in
    x = torch.randn(batch_size, d_in)

    trainer.sae.train()

    # set n_forward passes since fired to < dead feature window for all neurons
    trainer.n_forward_passes_since_fired = torch.ones_like(trainer.n_forward_passes_since_fired) * 3 * trainer.cfg.dead_feature_window  # type: ignore
    # then set the first 10 neurons to have fired recently
    trainer.n_forward_passes_since_fired[:10] = 0

    feature_acts = trainer.sae.encode(x)
    sae_out = trainer.sae.decode(feature_acts)

    _, hidden_pre = trainer.sae.encode_with_hidden_pre(x)
    ghost_grad_loss = trainer.calculate_ghost_grad_loss(
        x=x,
        sae_out=sae_out,
        per_item_mse_loss=trainer.mse_loss_fn(sae_out, x),
        hidden_pre=hidden_pre,
        dead_neuron_mask=trainer.dead_neurons,
    )
    ghost_grad_loss.backward()  # type: ignore

    # W_enc grad
    assert trainer.sae.W_enc.grad is not None
    assert torch.allclose(
        trainer.sae.W_enc.grad[:, :10], torch.zeros_like(trainer.sae.W_enc[:, :10])
    )
    assert trainer.sae.W_enc.grad[:, 10:].abs().sum() > 0.001

    # only features 1 and 3 should have non-zero gradients on the decoder weights
    assert trainer.sae.W_dec.grad is not None
    assert torch.allclose(
        trainer.sae.W_dec.grad[:10, :], torch.zeros_like(trainer.sae.W_dec[:10, :])
    )
    assert trainer.sae.W_dec.grad[10:, :].abs().sum() > 0.001


def test_per_item_mse_loss_with_norm_matches_original_implementation(
    trainer: SAETrainer,
) -> None:

    trainer.cfg.mse_loss_normalization = "dense_batch"
    trainer.mse_loss_fn = trainer._get_mse_loss_fn()

    input = torch.randn(3, 2)
    target = torch.randn(3, 2)
    target_centered = target - target.mean(dim=0, keepdim=True)
    orig_impl_res = (
        torch.pow((input - target.float()), 2)
        / (target_centered**2).sum(dim=-1, keepdim=True).sqrt()
    )
    sae_res = trainer.mse_loss_fn(
        input,
        target,
    )
    assert torch.allclose(orig_impl_res, sae_res, atol=1e-5)


def test_SparseAutoencoder_forward_can_add_noise_to_hidden_pre() -> None:
    clean_cfg = build_sae_cfg(d_in=2, d_sae=4, noise_scale=0)
    noisy_cfg = build_sae_cfg(d_in=2, d_sae=4, noise_scale=100)
    clean_sae = TrainingSparseAutoencoder(clean_cfg)
    noisy_sae = TrainingSparseAutoencoder(noisy_cfg)

    input = torch.randn(3, 2)

    clean_output1 = clean_sae.forward(input)
    clean_output2 = clean_sae.forward(input)
    noisy_output1 = noisy_sae.forward(input)
    noisy_output2 = noisy_sae.forward(input)

    # with no noise, the outputs should be identical
    assert torch.allclose(clean_output1, clean_output2)
    # noisy outputs should be different
    assert not torch.allclose(noisy_output1, noisy_output2)
    assert not torch.allclose(clean_output1, noisy_output1)


def test_SparseAutoencoder_remove_gradient_parallel_to_decoder_directions(
    trainer: SAETrainer,
) -> None:

    if not trainer.cfg.normalize_sae_decoder:
        pytest.skip("Test only applies when decoder is not normalized")
    sae = trainer.sae
    orig_grad = torch.randn_like(sae.W_dec)
    orig_W_dec = sae.W_dec.clone()
    sae.W_dec.grad = orig_grad.clone()
    sae.remove_gradient_parallel_to_decoder_directions()

    # check that the gradient is orthogonal to the decoder directions
    parallel_component = einops.einsum(
        sae.W_dec.grad,
        sae.W_dec.data,
        "d_sae d_in, d_sae d_in -> d_sae",
    )

    assert torch.allclose(
        parallel_component, torch.zeros_like(parallel_component), atol=1e-5
    )
    # the decoder weights should not have changed
    assert torch.allclose(sae.W_dec, orig_W_dec)

    # the gradient delta should align with the decoder directions
    grad_delta = orig_grad - sae.W_dec.grad
    assert torch.nn.functional.cosine_similarity(
        sae.W_dec.detach(), grad_delta, dim=1
    ).abs() == pytest.approx(1.0, abs=1e-3)


def test_SparseAutoencoder_set_decoder_norm_to_unit_norm(
    trainer: SAETrainer,
) -> None:

    if not trainer.cfg.normalize_sae_decoder:
        pytest.skip("Test only applies when decoder is not normalized")

    sae = trainer.sae
    sae.W_dec.data = 20 * torch.randn_like(sae.W_dec)
    sae.set_decoder_norm_to_unit_norm()
    assert torch.allclose(
        torch.norm(sae.W_dec, dim=1), torch.ones_like(sae.W_dec[:, 0])
    )
