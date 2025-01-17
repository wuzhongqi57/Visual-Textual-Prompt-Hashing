import torch
import torch.nn as nn
import torch.nn.functional as F
import math

from model.clip_model.model import load_download_clip, Transformer


class ResidualMLPs(nn.Module):
    """
    Residual MLPs
    ***D - ***D
    """

    def __init__(self, org_dim, dropout=0., num_layers=2, activation='relu'):
        super().__init__()
        self.num_layers = num_layers

        if activation == 'relu':
            self.activation_layer = nn.ReLU()
        elif activation == 'gelu':
            self.activation_layer = nn.GELU()
        else:
            pass

        self.mlps = nn.ModuleList(nn.Sequential(
            nn.Linear(org_dim, 4 * org_dim),
            self.activation_layer,
            nn.Dropout(p=dropout),
            nn.Linear(4 * org_dim, org_dim),
        ) for i in range(num_layers))

        self.lns = nn.ModuleList(nn.LayerNorm(org_dim) for i in range(num_layers))

    def forward(self, x):
        for i in range(self.num_layers):
            x = x + self.mlps[i](self.lns[i](x))
        return x


class PositionalEncoding(nn.Module):
    """
    Sin-cos position embedding
    LND - LND
    """

    def __init__(self, d_model, dropout=0., max_len=128):
        super(PositionalEncoding, self).__init__()

        self.dropout = nn.Dropout(p=dropout)

        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)

        pe = pe.unsqueeze(0).transpose(0, 1)  # [max-length, 1, d_model]
        pe = pe / (d_model ** 0.5)  #
        self.register_buffer('pe', pe)

    def forward(self, x):
        # x: LND
        x = x + self.pe[:x.size(0), :]
        return self.dropout(x)


class BitwiseHashing(nn.Module):
    """
    Bitwise hashing layer
    KND - NK
    """

    def __init__(self, org_dim, k_bits=32, ):
        super().__init__()
        self.k = k_bits
        self.fc_list = nn.ModuleList(nn.Linear(org_dim, 1) for _ in range(k_bits))

    def forward(self, x):
        # x: KND
        x = [self.fc_list[i](x[i, :, :]) for i in range(self.fc_list.__len__())]
        x = torch.stack(x)  # K,N,1
        x = x.permute(1, 0, 2)  # N,K,1
        x = torch.squeeze(x)  # N,K
        return torch.tanh(x)


class GlobalConceptLearning(nn.Module):
    """
    Concept Learning
    ***D - ***K
    """

    def __init__(self, k_concept, org_dim, dropout=0., activation='relu', res_mlp_layers=0):
        super().__init__()

        if res_mlp_layers != 0:
            self.mlp = ResidualMLPs(org_dim=org_dim, dropout=dropout, num_layers=res_mlp_layers, activation=activation)
        else:
            self.mlp = nn.Identity()

        self.common_concept_embedding = nn.Linear(org_dim, k_concept, bias=False)

    def forward(self, x):
        x = self.mlp(x)
        return x, torch.tanh(self.common_concept_embedding(x))


class LocalizedTokenAggregation(nn.Module):
    def __init__(self, top_k):
        super().__init__()
        self.top_k = top_k

    def my_top_k(self, top_k, x):
        val = torch.topk(x, k=top_k, dim=-1).values
        val_min = torch.min(val, dim=-1).values
        val_min = val_min.unsqueeze(-1).repeat(1, 1, x.shape[2])

        ge_ = torch.ge(x, val_min)  # torch.ge: Compute input >= other

        neg_inf = torch.zeros_like(x)
        neg_inf = neg_inf.fill_(float("-inf"))
        result = torch.where(ge_, x, neg_inf)
        return result

    def gen_top_k_label(self, top_k, x):
        top_k_val_with_neg_inf = self.my_top_k(top_k, x)

        zeros = torch.zeros_like(x)
        ones = torch.ones_like(x)
        pseudo_label = torch.where(top_k_val_with_neg_inf > 0, ones, zeros)

        return pseudo_label, top_k_val_with_neg_inf

    def forward(self, x, token_concept_embedding, key_padding_mask=None):
        # x: LND
        # token_concept_embedding: LNK(K concept)
        # return: KND
        sim = token_concept_embedding.detach()  # no grad need.

        if key_padding_mask is not None:
            # set sim to '-inf' by key padding mask
            key_pad = torch.where(key_padding_mask, float('-inf'), 0.)  # NL
            key_pad = key_pad.unsqueeze(dim=1).repeat(1, sim.shape[2], 1)  # NKL
            key_pad = key_pad.permute(2, 0, 1)  # LNK

            sim += key_pad

        # make neg_value to neg_inf
        neg_inf = torch.zeros_like(sim)
        neg_inf = neg_inf.fill_(float("-inf"))
        sim = torch.where(sim > 0, sim, neg_inf)

        # select top_k for each token
        pseudo_label, sim = self.gen_top_k_label(self.top_k, sim)

        # softmax
        sim = torch.softmax(sim, dim=0)  # sim: LNK
        sim = torch.where(torch.isnan(sim), torch.full_like(sim, 0), sim)  # sim: LNK

        # x: LND
        # sim: LNK
        merge_val = torch.bmm(
            sim.permute(1, 2, 0),  # NKL
            x.permute(1, 0, 2)  # NLD
        )  # NKD
        merge_val = merge_val.permute(1, 0, 2)  # NKD - KND
        return merge_val, pseudo_label  # KND, LNK


class LocalConceptTransforming(nn.Module):
    def __init__(self, clip_embed_dim, k_bits, transformer_layers, dropout, top_k):
        super().__init__()
        self.lta = LocalizedTokenAggregation(top_k=top_k)
        self.position = PositionalEncoding(clip_embed_dim, dropout=dropout, max_len=k_bits)
        self.transformer = Transformer(
            width=clip_embed_dim,
            layers=transformer_layers,
            heads=clip_embed_dim // 64,
        )
        self.hashing = BitwiseHashing(org_dim=clip_embed_dim, k_bits=k_bits)

    def forward(self, x, token_concept_embedding, key_padding_mask=None):
        # x: LND
        # token_concept_embedding: LNK (K concept)
        x, pseudo_label = self.lta(x, token_concept_embedding, key_padding_mask)
        x, _ = self.transformer(self.position(x))
        return self.hashing(x), pseudo_label, x


class HashingModel(nn.Module):
    """
    Hashing model
    """

    def __init__(self, clip_info=None, args=None):
        super().__init__()

        self.k_bits = k_bits = args.k_bits
        self.dropout = dropout = args.dropout
        self.transformer_layers = transformer_layers = args.transformer_layers
        self.activation = activation = args.activation
        self.top_k_label = top_k_label = args.top_k_label
        self.res_mlp_layers = res_mlp_layers = args.res_mlp_layers

        clip_embed_dim = clip_info['embed_dim']

        # share weight.
        self.gcl_i = self.gcl_t = GlobalConceptLearning(k_concept=k_bits, org_dim=clip_embed_dim, dropout=dropout,
                                                        activation=activation, res_mlp_layers=res_mlp_layers)

        self.lct_i = LocalConceptTransforming(clip_embed_dim=clip_embed_dim, k_bits=k_bits,
                                              transformer_layers=transformer_layers, dropout=0,
                                              top_k=top_k_label)
        self.lct_t = LocalConceptTransforming(clip_embed_dim=clip_embed_dim, k_bits=k_bits,
                                              transformer_layers=transformer_layers, dropout=0,
                                              top_k=top_k_label)

        self.img_concept_proj = nn.Linear(clip_embed_dim, clip_embed_dim)
        self.txt_concept_proj = nn.Linear(clip_embed_dim, clip_embed_dim)

    def forward(self, img_tokens, txt_tokens, img_cls, txt_eos, key_padding_mask):
        output_dict = {}

        gcl_i = self.gcl_i
        gcl_t = self.gcl_t
        lct_i = self.lct_i
        lct_t = self.lct_t

        res_img_cls, img_cls_hash = gcl_i(img_cls)
        res_txt_cls, txt_cls_hash = gcl_t(txt_eos)

        output_dict['img_cls_hash'] = img_cls_hash
        output_dict['txt_cls_hash'] = txt_cls_hash

        output_dict['res_img_cls'] = F.normalize(res_img_cls, dim=-1)
        output_dict['res_txt_cls'] = F.normalize(res_txt_cls, dim=-1)

        tokens_hash_i, _, trans_tokens_i = lct_i(img_tokens, gcl_i(img_tokens)[1].detach())
        tokens_hash_t, _, trans_tokens_t = lct_t(txt_tokens, gcl_t(txt_tokens)[1].detach(), key_padding_mask)

        output_dict['img_tokens_hash'] = tokens_hash_i
        output_dict['txt_tokens_hash'] = tokens_hash_t

        output_dict['trans_tokens_i'] = F.normalize(self.img_concept_proj(trans_tokens_i), dim=-1)
        output_dict['trans_tokens_t'] = F.normalize(self.txt_concept_proj(trans_tokens_t), dim=-1)

        return output_dict

    def forward_image(self, img_tokens, img_cls):
        output_dict = {}

        res_img_cls, img_cls_hash = self.gcl_i(img_cls)
        output_dict['img_cls_hash'] = img_cls_hash
        output_dict['res_img_cls'] = F.normalize(res_img_cls, dim=-1)

        tokens_hash_i, _, trans_tokens_i = self.lct_i(img_tokens, self.gcl_i(img_tokens)[1].detach())
        output_dict['img_tokens_hash'] = tokens_hash_i
        output_dict['trans_tokens_i'] = F.normalize(self.img_concept_proj(trans_tokens_i), dim=-1)

        return output_dict

    def forward_text(self, txt_tokens, txt_eos, key_padding_mask):
        output_dict = {}

        res_txt_cls, txt_cls_hash = self.gcl_t(txt_eos)
        output_dict['txt_cls_hash'] = txt_cls_hash
        output_dict['res_txt_cls'] = F.normalize(res_txt_cls, dim=-1)

        tokens_hash_t, _, trans_tokens_t = self.lct_t(txt_tokens, self.gcl_t(txt_tokens)[1].detach(), key_padding_mask)
        output_dict['txt_tokens_hash'] = tokens_hash_t
        output_dict['trans_tokens_t'] = F.normalize(self.txt_concept_proj(trans_tokens_t), dim=-1)

        return output_dict


class VisualPrompting(nn.Module):
    def __init__(self, dim=512, ):
        super().__init__()
        self.fc = nn.Sequential(
            nn.Linear(dim, dim),
            nn.Softmax(),
        )

    def forward(self, img_cls, txt_tokens):
        cls_weight = self.fc(img_cls)
        return cls_weight * txt_tokens


class TextualPrompting(nn.Module):
    def __init__(self, dim=512, dropout=0.2):
        super().__init__()
        self.mlp = nn.Sequential(
            nn.Linear(dim, int(1 / 4 * dim)),
            nn.ReLU(),
            nn.Dropout(p=dropout),
            nn.Linear(int(1 / 4 * dim), dim)
        )

    def forward(self, txt_eos):
        return txt_eos + self.mlp(txt_eos)


class VisualTextualPrompting(nn.Module):
    def __init__(self, args, dim=512, transformer_layers=2, heads=16):
        super().__init__()
        self.args = args
        self.vap = VisualPrompting()
        self.tap = TextualPrompting()
        # Adopt additional 2-layers transformer to simulate the situation of unfreezing the last two layers of the
        # backbone to enhance the representation ability of the model
        self.te_i = Transformer(heads=heads, layers=transformer_layers, width=dim)
        self.te_t = Transformer(heads=heads, layers=transformer_layers, width=dim)

    def forward(self, img_tokens, img_cls, txt_tokens, txt_eos, cap_tokens):
        txt_tokens_prompted = self.vap(img_cls, txt_tokens)
        txt_eos_prompted = self.tap(txt_eos)

        img_tokens = img_tokens.permute(1, 0, 2)
        txt_tokens_prompted = txt_tokens_prompted.permute(1, 0, 2)
        cap_tokens = cap_tokens.permute(1, 0, 2)

        img_x = torch.cat([img_cls.unsqueeze(1), img_tokens], dim=1).permute(1, 0, 2)
        txt_x = torch.cat([txt_eos_prompted.unsqueeze(1), txt_tokens_prompted, cap_tokens], dim=1).permute(1, 0, 2)

        # Re-enhance
        img_y, _ = self.te_i(img_x)
        txt_y, _ = self.te_t(txt_x)

        # Discarding image caption tokens
        txt_tokens_length = txt_tokens_prompted.size(1)

        return img_y[1:], img_y[0], txt_y[1:1 + txt_tokens_length], txt_y[0], txt_eos_prompted

    def forward_text(self, txt_tokens, txt_eos):
        txt_eos = self.tap(txt_eos)
        txt_x = torch.cat([txt_eos.unsqueeze(1), txt_tokens.permute(1, 0, 2)], dim=1).permute(1, 0, 2)
        txt_y, _ = self.te_t(txt_x)
        return txt_y[1:], txt_y[0]

    def forward_image(self, img_tokens, img_cls):
        img_x = torch.cat([img_cls.unsqueeze(1), img_tokens.permute(1, 0, 2)], dim=1).permute(1, 0, 2)
        img_y, _ = self.te_i(img_x)
        return img_y[1:], img_y[0]


class MITH(nn.Module):
    def __init__(self, args=None):
        super(MITH, self).__init__()
        self.args = args
        self.clip, clip_info = load_download_clip(self.args.clip_path)
        self.hash = HashingModel(clip_info=clip_info, args=args)

    def forward(self, image, text, key_padding_mask):
        img_tokens, _, img_cls = self.clip.encode_image(image)
        txt_tokens, _, new_key_padding_mask, txt_eos = self.clip.encode_text(text, key_padding_mask)
        output_dict = self.hash(img_tokens, txt_tokens, img_cls, txt_eos, new_key_padding_mask)
        return output_dict

    def forward_text(self, text, key_padding_mask):
        txt_tokens, _, new_key_padding_mask, txt_eos = self.clip.encode_text(text, key_padding_mask)
        output_dict = self.hash(txt_tokens, txt_eos, new_key_padding_mask)
        return output_dict

    def forward_image(self, image):
        img_tokens, _, img_cls = self.clip.encode_image(image)
        output_dict = self.hash(img_tokens, img_cls)
        return output_dict
