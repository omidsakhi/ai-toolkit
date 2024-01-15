import torch
from typing import Literal, Optional

from toolkit.basic import value_map
from toolkit.data_transfer_object.data_loader import DataLoaderBatchDTO
from toolkit.prompt_utils import PromptEmbeds, concat_prompt_embeds
from toolkit.stable_diffusion_model import StableDiffusion
from toolkit.train_tools import get_torch_dtype

GuidanceType = Literal["targeted", "polarity", "targeted_polarity", "direct"]

DIFFERENTIAL_SCALER = 0.2


# DIFFERENTIAL_SCALER = 0.25


def get_differential_mask(
        conditional_latents: torch.Tensor,
        unconditional_latents: torch.Tensor,
        threshold: float = 0.2,
        gradient: bool = False,
):
    # make a differential mask
    differential_mask = torch.abs(conditional_latents - unconditional_latents)
    max_differential = \
        differential_mask.max(dim=1, keepdim=True)[0].max(dim=2, keepdim=True)[0].max(dim=3, keepdim=True)[0]
    differential_scaler = 1.0 / max_differential
    differential_mask = differential_mask * differential_scaler

    if gradient:
        # wew need to scale it to 0-1
        # differential_mask = differential_mask - differential_mask.min()
        # differential_mask = differential_mask / differential_mask.max()
        # add 0.2 threshold to both sides and clip
        differential_mask = value_map(
            differential_mask,
            differential_mask.min(),
            differential_mask.max(),
            0 - threshold,
            1 + threshold
        )
        differential_mask = torch.clamp(differential_mask, 0.0, 1.0)
    else:

        # make everything less than 0.2 be 0.0 and everything else be 1.0
        differential_mask = torch.where(
            differential_mask < threshold,
            torch.zeros_like(differential_mask),
            torch.ones_like(differential_mask)
        )
    return differential_mask


def get_targeted_polarity_loss(
        noisy_latents: torch.Tensor,
        conditional_embeds: PromptEmbeds,
        match_adapter_assist: bool,
        network_weight_list: list,
        timesteps: torch.Tensor,
        pred_kwargs: dict,
        batch: 'DataLoaderBatchDTO',
        noise: torch.Tensor,
        sd: 'StableDiffusion',
        **kwargs
):
    dtype = get_torch_dtype(sd.torch_dtype)
    device = sd.device_torch
    with torch.no_grad():
        conditional_latents = batch.latents.to(device, dtype=dtype).detach()
        unconditional_latents = batch.unconditional_latents.to(device, dtype=dtype).detach()

        # inputs_abs_mean = torch.abs(conditional_latents).mean(dim=[1, 2, 3], keepdim=True)
        # noise_abs_mean = torch.abs(noise).mean(dim=[1, 2, 3], keepdim=True)
        differential_scaler = DIFFERENTIAL_SCALER

        unconditional_diff = (unconditional_latents - conditional_latents)
        unconditional_diff_noise = unconditional_diff * differential_scaler
        conditional_diff = (conditional_latents - unconditional_latents)
        conditional_diff_noise = conditional_diff * differential_scaler
        conditional_diff_noise = conditional_diff_noise.detach().requires_grad_(False)
        unconditional_diff_noise = unconditional_diff_noise.detach().requires_grad_(False)
        #
        baseline_conditional_noisy_latents = sd.add_noise(
            conditional_latents,
            noise,
            timesteps
        ).detach()

        baseline_unconditional_noisy_latents = sd.add_noise(
            unconditional_latents,
            noise,
            timesteps
        ).detach()

        conditional_noise = noise + unconditional_diff_noise
        unconditional_noise = noise + conditional_diff_noise

        conditional_noisy_latents = sd.add_noise(
            conditional_latents,
            conditional_noise,
            timesteps
        ).detach()

        unconditional_noisy_latents = sd.add_noise(
            unconditional_latents,
            unconditional_noise,
            timesteps
        ).detach()

        # double up everything to run it through all at once
        cat_embeds = concat_prompt_embeds([conditional_embeds, conditional_embeds])
        cat_latents = torch.cat([conditional_noisy_latents, unconditional_noisy_latents], dim=0)
        cat_timesteps = torch.cat([timesteps, timesteps], dim=0)
        # cat_baseline_noisy_latents = torch.cat(
        #     [baseline_conditional_noisy_latents, baseline_unconditional_noisy_latents],
        #     dim=0
        # )

        # Disable the LoRA network so we can predict parent network knowledge without it
        # sd.network.is_active = False
        # sd.unet.eval()

        # Predict noise to get a baseline of what the parent network wants to do with the latents + noise.
        # This acts as our control to preserve the unaltered parts of the image.
        # baseline_prediction = sd.predict_noise(
        #     latents=cat_baseline_noisy_latents.to(device, dtype=dtype).detach(),
        #     conditional_embeddings=cat_embeds.to(device, dtype=dtype).detach(),
        #     timestep=cat_timesteps,
        #     guidance_scale=1.0,
        #     **pred_kwargs  # adapter residuals in here
        # ).detach()

        # conditional_baseline_prediction, unconditional_baseline_prediction = torch.chunk(baseline_prediction, 2, dim=0)

        # negative_network_weights = [weight * -1.0 for weight in network_weight_list]
        # positive_network_weights = [weight * 1.0 for weight in network_weight_list]
        # cat_network_weight_list = positive_network_weights + negative_network_weights

        # turn the LoRA network back on.
        sd.unet.train()
        # sd.network.is_active = True

        # sd.network.multiplier = cat_network_weight_list

    # do our prediction with LoRA active on the scaled guidance latents
    prediction = sd.predict_noise(
        latents=cat_latents.to(device, dtype=dtype).detach(),
        conditional_embeddings=cat_embeds.to(device, dtype=dtype).detach(),
        timestep=cat_timesteps,
        guidance_scale=1.0,
        **pred_kwargs  # adapter residuals in here
    )

    # prediction = prediction - baseline_prediction

    pred_pos, pred_neg = torch.chunk(prediction, 2, dim=0)
    # pred_pos = pred_pos - conditional_baseline_prediction
    # pred_neg = pred_neg - unconditional_baseline_prediction

    pred_loss = torch.nn.functional.mse_loss(
        pred_pos.float(),
        conditional_noise.float(),
        reduction="none"
    )
    pred_loss = pred_loss.mean([1, 2, 3])

    pred_neg_loss = torch.nn.functional.mse_loss(
        pred_neg.float(),
        unconditional_noise.float(),
        reduction="none"
    )
    pred_neg_loss = pred_neg_loss.mean([1, 2, 3])

    loss = pred_loss + pred_neg_loss

    loss = loss.mean()
    loss.backward()

    # detach it so parent class can run backward on no grads without throwing error
    loss = loss.detach()
    loss.requires_grad_(True)

    return loss

def get_direct_guidance_loss(
        noisy_latents: torch.Tensor,
        conditional_embeds: 'PromptEmbeds',
        match_adapter_assist: bool,
        network_weight_list: list,
        timesteps: torch.Tensor,
        pred_kwargs: dict,
        batch: 'DataLoaderBatchDTO',
        noise: torch.Tensor,
        sd: 'StableDiffusion',
        unconditional_embeds: Optional[PromptEmbeds] = None,
        mask_multiplier=None,
        prior_pred=None,
        **kwargs
):
    with torch.no_grad():
        # Perform targeted guidance (working title)
        dtype = get_torch_dtype(sd.torch_dtype)
        device = sd.device_torch


        conditional_latents = batch.latents.to(device, dtype=dtype).detach()
        unconditional_latents = batch.unconditional_latents.to(device, dtype=dtype).detach()

        conditional_noisy_latents = sd.add_noise(
            conditional_latents,
            # target_noise,
            noise,
            timesteps
        ).detach()

        unconditional_noisy_latents = sd.add_noise(
            unconditional_latents,
            noise,
            timesteps
        ).detach()
        # turn the LoRA network back on.
        sd.unet.train()
        # sd.network.is_active = True

        # sd.network.multiplier = network_weight_list
    # do our prediction with LoRA active on the scaled guidance latents
    if unconditional_embeds is not None:
        unconditional_embeds = unconditional_embeds.to(device, dtype=dtype).detach()
        unconditional_embeds = concat_prompt_embeds([unconditional_embeds, unconditional_embeds])

    prediction = sd.predict_noise(
        latents=torch.cat([unconditional_noisy_latents, conditional_noisy_latents]).to(device, dtype=dtype).detach(),
        conditional_embeddings=concat_prompt_embeds([conditional_embeds,conditional_embeds]).to(device, dtype=dtype).detach(),
        unconditional_embeddings=unconditional_embeds,
        timestep=torch.cat([timesteps, timesteps]),
        guidance_scale=1.0,
        **pred_kwargs  # adapter residuals in here
    )

    noise_pred_uncond, noise_pred_cond = torch.chunk(prediction, 2, dim=0)

    guidance_scale = 1.0
    guidance_pred = noise_pred_uncond + guidance_scale * (
            noise_pred_cond - noise_pred_uncond
    )

    guidance_loss = torch.nn.functional.mse_loss(
        guidance_pred.float(),
        noise.detach().float(),
        reduction="none"
    )
    if mask_multiplier is not None:
        guidance_loss = guidance_loss * mask_multiplier

    guidance_loss = guidance_loss.mean([1, 2, 3])

    guidance_loss = guidance_loss.mean()

    # loss = guidance_loss + masked_noise_loss
    loss = guidance_loss

    loss.backward()

    # detach it so parent class can run backward on no grads without throwing error
    loss = loss.detach()
    loss.requires_grad_(True)

    return loss


# targeted
def get_targeted_guidance_loss(
        noisy_latents: torch.Tensor,
        conditional_embeds: 'PromptEmbeds',
        match_adapter_assist: bool,
        network_weight_list: list,
        timesteps: torch.Tensor,
        pred_kwargs: dict,
        batch: 'DataLoaderBatchDTO',
        noise: torch.Tensor,
        sd: 'StableDiffusion',
        **kwargs
):
    with torch.no_grad():
        dtype = get_torch_dtype(sd.torch_dtype)
        device = sd.device_torch

        conditional_latents = batch.latents.to(device, dtype=dtype).detach()
        unconditional_latents = batch.unconditional_latents.to(device, dtype=dtype).detach()

        # Encode the unconditional image into latents
        unconditional_noisy_latents = sd.noise_scheduler.add_noise(
            unconditional_latents,
            noise,
            timesteps
        )
        conditional_noisy_latents = sd.noise_scheduler.add_noise(
            conditional_latents,
            noise,
            timesteps
        )

        # was_network_active = self.network.is_active
        sd.network.is_active = False
        sd.unet.eval()

        target_differential = unconditional_latents - conditional_latents
        # scale our loss by the differential scaler
        target_differential_abs = target_differential.abs()
        target_differential_abs_min = \
        target_differential_abs.min(dim=1, keepdim=True)[0].max(dim=2, keepdim=True)[0].max(dim=3, keepdim=True)[0]
        target_differential_abs_max = \
            target_differential_abs.max(dim=1, keepdim=True)[0].max(dim=2, keepdim=True)[0].max(dim=3, keepdim=True)[0]

        min_guidance = 1.0
        max_guidance = 2.0

        differential_scaler = value_map(
            target_differential_abs,
            target_differential_abs_min,
            target_differential_abs_max,
            min_guidance,
            max_guidance
        ).detach()


        # With LoRA network bypassed, predict noise to get a baseline of what the network
        # wants to do with the latents + noise. Pass our target latents here for the input.
        target_unconditional = sd.predict_noise(
            latents=unconditional_noisy_latents.to(device, dtype=dtype).detach(),
            conditional_embeddings=conditional_embeds.to(device, dtype=dtype).detach(),
            timestep=timesteps,
            guidance_scale=1.0,
            **pred_kwargs  # adapter residuals in here
        ).detach()
        prior_prediction_loss = torch.nn.functional.mse_loss(
            target_unconditional.float(),
            noise.float(),
            reduction="none"
        ).detach().clone()

    # turn the LoRA network back on.
    sd.unet.train()
    sd.network.is_active = True
    sd.network.multiplier = network_weight_list + [x + -1.0 for x in network_weight_list]

    # with LoRA active, predict the noise with the scaled differential latents added. This will allow us
    # the opportunity to predict the differential + noise that was added to the latents.
    prediction = sd.predict_noise(
        latents=torch.cat([conditional_noisy_latents, unconditional_noisy_latents], dim=0).to(device, dtype=dtype).detach(),
        conditional_embeddings=concat_prompt_embeds([conditional_embeds, conditional_embeds]).to(device, dtype=dtype).detach(),
        timestep=torch.cat([timesteps, timesteps], dim=0),
        guidance_scale=1.0,
        **pred_kwargs  # adapter residuals in here
    )

    prediction_conditional, prediction_unconditional = torch.chunk(prediction, 2, dim=0)

    conditional_loss = torch.nn.functional.mse_loss(
        prediction_conditional.float(),
        noise.float(),
        reduction="none"
    )

    unconditional_loss = torch.nn.functional.mse_loss(
        prediction_unconditional.float(),
        noise.float(),
        reduction="none"
    )

    positive_loss = torch.abs(
        conditional_loss.float() - prior_prediction_loss.float(),
    )
    # scale our loss by the differential scaler
    positive_loss = positive_loss * differential_scaler

    positive_loss = positive_loss.mean([1, 2, 3])

    polar_loss = torch.abs(
        conditional_loss.float() - unconditional_loss.float(),
    ).mean([1, 2, 3])


    positive_loss = positive_loss.mean() + polar_loss.mean()


    positive_loss.backward()
    # loss = positive_loss.detach() + negative_loss.detach()
    loss = positive_loss.detach()

    # add a grad so other backward does not fail
    loss.requires_grad_(True)

    # restore network
    sd.network.multiplier = network_weight_list

    return loss

def get_guided_loss_polarity(
        noisy_latents: torch.Tensor,
        conditional_embeds: PromptEmbeds,
        match_adapter_assist: bool,
        network_weight_list: list,
        timesteps: torch.Tensor,
        pred_kwargs: dict,
        batch: 'DataLoaderBatchDTO',
        noise: torch.Tensor,
        sd: 'StableDiffusion',
        **kwargs
):
    dtype = get_torch_dtype(sd.torch_dtype)
    device = sd.device_torch
    with torch.no_grad():
        dtype = get_torch_dtype(dtype)
        noise = noise.to(device, dtype=dtype).detach()

        conditional_latents = batch.latents.to(device, dtype=dtype).detach()
        unconditional_latents = batch.unconditional_latents.to(device, dtype=dtype).detach()

        conditional_noisy_latents = sd.add_noise(
            conditional_latents,
            noise,
            timesteps
        ).detach()

        unconditional_noisy_latents = sd.add_noise(
            unconditional_latents,
            noise,
            timesteps
        ).detach()

        # double up everything to run it through all at once
        cat_embeds = concat_prompt_embeds([conditional_embeds, conditional_embeds])
        cat_latents = torch.cat([conditional_noisy_latents, unconditional_noisy_latents], dim=0)
        cat_timesteps = torch.cat([timesteps, timesteps], dim=0)

        negative_network_weights = [weight * -1.0 for weight in network_weight_list]
        positive_network_weights = [weight * 1.0 for weight in network_weight_list]
        cat_network_weight_list = positive_network_weights + negative_network_weights

        # turn the LoRA network back on.
        sd.unet.train()
        sd.network.is_active = True

        sd.network.multiplier = cat_network_weight_list

    # do our prediction with LoRA active on the scaled guidance latents
    prediction = sd.predict_noise(
        latents=cat_latents.to(device, dtype=dtype).detach(),
        conditional_embeddings=cat_embeds.to(device, dtype=dtype).detach(),
        timestep=cat_timesteps,
        guidance_scale=1.0,
        **pred_kwargs  # adapter residuals in here
    )

    pred_pos, pred_neg = torch.chunk(prediction, 2, dim=0)

    pred_loss = torch.nn.functional.mse_loss(
        pred_pos.float(),
        noise.float(),
        reduction="none"
    )
    # pred_loss = pred_loss.mean([1, 2, 3])

    pred_neg_loss = torch.nn.functional.mse_loss(
        pred_neg.float(),
        noise.float(),
        reduction="none"
    )

    loss = pred_loss + pred_neg_loss

    loss = loss.mean([1, 2, 3])
    loss = loss.mean()
    loss.backward()

    # detach it so parent class can run backward on no grads without throwing error
    loss = loss.detach()
    loss.requires_grad_(True)

    return loss


# this processes all guidance losses based on the batch information
def get_guidance_loss(
        noisy_latents: torch.Tensor,
        conditional_embeds: 'PromptEmbeds',
        match_adapter_assist: bool,
        network_weight_list: list,
        timesteps: torch.Tensor,
        pred_kwargs: dict,
        batch: 'DataLoaderBatchDTO',
        noise: torch.Tensor,
        sd: 'StableDiffusion',
        unconditional_embeds: Optional[PromptEmbeds] = None,
        mask_multiplier=None,
        prior_pred=None,
        **kwargs
):
    # TODO add others and process individual batch items separately
    guidance_type: GuidanceType = batch.file_items[0].dataset_config.guidance_type

    if guidance_type == "targeted":
        assert unconditional_embeds is None, "Unconditional embeds are not supported for targeted guidance"
        return get_targeted_guidance_loss(
            noisy_latents,
            conditional_embeds,
            match_adapter_assist,
            network_weight_list,
            timesteps,
            pred_kwargs,
            batch,
            noise,
            sd,
            **kwargs
        )
    elif guidance_type == "polarity":
        assert unconditional_embeds is None, "Unconditional embeds are not supported for polarity guidance"
        return get_guided_loss_polarity(
            noisy_latents,
            conditional_embeds,
            match_adapter_assist,
            network_weight_list,
            timesteps,
            pred_kwargs,
            batch,
            noise,
            sd,
            **kwargs
        )

    elif guidance_type == "targeted_polarity":
        assert unconditional_embeds is None, "Unconditional embeds are not supported for targeted polarity guidance"
        return get_targeted_polarity_loss(
            noisy_latents,
            conditional_embeds,
            match_adapter_assist,
            network_weight_list,
            timesteps,
            pred_kwargs,
            batch,
            noise,
            sd,
            **kwargs
        )
    elif guidance_type == "direct":
        return get_direct_guidance_loss(
            noisy_latents,
            conditional_embeds,
            match_adapter_assist,
            network_weight_list,
            timesteps,
            pred_kwargs,
            batch,
            noise,
            sd,
            unconditional_embeds=unconditional_embeds,
            mask_multiplier=mask_multiplier,
            prior_pred=prior_pred,
            **kwargs
        )
    else:
        raise NotImplementedError(f"Guidance type {guidance_type} is not implemented")
