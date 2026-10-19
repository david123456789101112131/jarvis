"""Tiny, randomly initialised stand-ins for the local media models, laid out as `jarvis media install` lays out the
real ones, so the real worker (diffusers' real ZImagePipeline and WanPipeline code) can run end to end on a
processor in seconds. They make noise, not pictures: what is checked is the path from request to PNG and MP4.

Run with a Python that has torch, diffusers and transformers (the media runtime):
    python tests/fixtures/tiny_media.py <folder>
Nothing is downloaded: the tokenizers are built here too.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

IMAGE_DIR = "Z-Image-Turbo"
VIDEO_DIR = "Wan2.2-TI2V-5B"
CHAT = ("{% for m in messages %}<|im_start|>{{ m['role'] }}\n{{ m['content'] }}<|im_end|>\n{% endfor %}"
        "{% if add_generation_prompt %}<|im_start|>assistant\n{% endif %}")


def _tokenizer(chat: bool):  # type: ignore[no-untyped-def]
    from tokenizers import Tokenizer, decoders, models, pre_tokenizers
    from transformers import PreTrainedTokenizerFast

    # Bytes as tokens: any text (Hebrew too) tokenizes, with no vocabulary file to download.
    specials = ["<pad>", "</s>", "<unk>", "<|im_start|>", "<|im_end|>"]
    alphabet = pre_tokenizers.ByteLevel.alphabet()
    vocab = {tok: i for i, tok in enumerate([*specials, *alphabet])}
    tok = Tokenizer(models.BPE(vocab=vocab, merges=[], unk_token="<unk>"))
    tok.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    tok.decoder = decoders.ByteLevel()
    fast = PreTrainedTokenizerFast(tokenizer_object=tok, pad_token="<pad>", eos_token="</s>", unk_token="<unk>",
                                   additional_special_tokens=["<|im_start|>", "<|im_end|>"])
    if chat:
        fast.chat_template = CHAT
    return fast, len(vocab)


def build_image(folder: Path) -> None:
    import torch
    from diffusers import (
        AutoencoderKL,
        FlowMatchEulerDiscreteScheduler,
        ZImagePipeline,
        ZImageTransformer2DModel,
    )
    from transformers import Qwen3Config, Qwen3Model

    torch.manual_seed(0)
    tokenizer, vocab = _tokenizer(chat=True)
    text_encoder = Qwen3Model(Qwen3Config(vocab_size=vocab, hidden_size=32, intermediate_size=64, num_hidden_layers=2,
                                          num_attention_heads=2, num_key_value_heads=1, head_dim=16,
                                          max_position_embeddings=1024))
    transformer = ZImageTransformer2DModel(all_patch_size=(2,), all_f_patch_size=(1,), in_channels=4, dim=48,
                                           n_layers=1, n_refiner_layers=1, n_heads=2, n_kv_heads=2, cap_feat_dim=32,
                                           axes_dims=[8, 8, 8], axes_lens=[1024, 512, 512])
    vae = AutoencoderKL(in_channels=3, out_channels=3, down_block_types=("DownEncoderBlock2D", "DownEncoderBlock2D"),
                        up_block_types=("UpDecoderBlock2D", "UpDecoderBlock2D"), block_out_channels=(8, 16),
                        latent_channels=4, layers_per_block=1, norm_num_groups=4, scaling_factor=1.0, shift_factor=0.0)
    pipe = ZImagePipeline(scheduler=FlowMatchEulerDiscreteScheduler(), vae=vae, text_encoder=text_encoder,
                          tokenizer=tokenizer, transformer=transformer)
    pipe.save_pretrained(str(folder))


def build_video(folder: Path) -> None:
    import torch
    from diffusers import (
        AutoencoderKLWan,
        FlowMatchEulerDiscreteScheduler,
        WanPipeline,
        WanTransformer3DModel,
    )
    from transformers import UMT5Config, UMT5EncoderModel

    torch.manual_seed(0)
    tokenizer, vocab = _tokenizer(chat=False)
    text_encoder = UMT5EncoderModel(UMT5Config(vocab_size=vocab, d_model=32, d_kv=16, d_ff=64, num_layers=1,
                                               num_heads=2, relative_attention_num_buckets=8))
    transformer = WanTransformer3DModel(patch_size=(1, 2, 2), num_attention_heads=2, attention_head_dim=12,
                                        in_channels=16, out_channels=16, text_dim=32, freq_dim=256, ffn_dim=32,
                                        num_layers=1, cross_attn_norm=True, qk_norm="rms_norm_across_heads",
                                        rope_max_seq_len=32)
    vae = AutoencoderKLWan(base_dim=3, z_dim=16, dim_mult=[1, 1, 1, 1], num_res_blocks=1,
                           temperal_downsample=[False, True, True])
    pipe = WanPipeline(tokenizer=tokenizer, text_encoder=text_encoder, vae=vae, transformer=transformer,
                       scheduler=FlowMatchEulerDiscreteScheduler(shift=5.0), expand_timesteps=True)
    pipe.save_pretrained(str(folder))
    index = folder / "model_index.json"
    data = json.loads(index.read_text(encoding="utf-8"))
    assert data.get("expand_timesteps") is True, data      # as Wan 2.2 TI2V-5B's own model_index.json says


def build(root: Path) -> None:
    build_image(root / IMAGE_DIR)
    build_video(root / VIDEO_DIR)


if __name__ == "__main__":
    build(Path(sys.argv[1]))
    print("ok")
