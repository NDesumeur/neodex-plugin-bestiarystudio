# -*- coding: utf-8 -*-
"""NeoBestiary Studio - moteur d'image moderne (FLUX.2 klein 4B / Z-Image Turbo, nœuds natifs de ComfyUI).

Le texte de l'utilisateur part tel quel (l'encodeur Qwen3 comprend le français) : pas de lexique, pas de
pondérations, pas de négatifs imposés. FLUX.2 klein sait aussi éditer à partir d'images de référence
(ReferenceLatent) : c'est ce qui fait tourner la créature et l'anime en gardant son design.
"""
import math
import re
import uuid

import numpy as np
from PIL import Image, ImageChops, ImageOps
from scipy import ndimage

import nb_archetypes
import nb_auto
import nb_comfy as nbc
import nb_core
import nb_guides

ENCODER = "qwen_3_4b.safetensors"
MODELS = {
    "klein": dict(unet="flux-2-klein-4b.safetensors", vae="flux2-vae.safetensors", clip_type="flux2", steps=4),
    "zimage": dict(unet="z_image_turbo_int8_convrot.safetensors", vae="ae.safetensors", clip_type="lumina2", steps=8),
}
SIZE = 1024
CUT_MODEL = "birefnet.safetensors"  # détourage (models/background_removal) : fond et ombre portée retirés

STYLE = ("Pixel art sprite of a single creature for a top-down RPG, seen from above at a three-quarter angle "
         "(camera tilted about 30 degrees down). Entire creature visible, centered, crisp pixels, clean dark outline, "
         "flat plain light gray background, no text, no scenery, no cast shadow on the ground.")

# Orientation stricte : les jeux à 4 directions veulent face / dos / profil francs, pas un corps tourné de biais.
FACING = {
    "bas": ("It faces straight toward the bottom of the screen, perfectly frontal and symmetrical: its body is aligned "
            "on the vertical axis of the image, not turned to the left or to the right at all."),
    "haut": ("Show this exact same creature seen exactly from behind: it walks straight up toward the top of the screen, "
             "perfectly symmetrical, its body aligned on the vertical axis, not turned to either side. We see its back "
             "and the back of its head; no face and no eyes are visible. The reference image is already flipped "
             "left-right as seen from behind: keep every limb, wing, weapon and held object on the same side of the "
             "image as in the reference."),
    "gauche": ("Turn this exact same creature a full 90 degrees so it faces exactly toward the left edge of the screen: "
               "a pure side profile, in exactly the same posture as in the reference image{posture}, its face pointing left, "
               "nothing of it turned toward the viewer."),
}
KEEP = (" Keep the identical design, colors, proportions and pixel art style, and the same top-down camera. "
        "Keep exactly the same body parts and the same number of {parts}: add nothing, remove nothing. "
        "Plain flat light gray background, no cast shadow.")
GUESSED_POSTURE = " (a four-legged creature stays on all fours, an upright one stays upright)"


# parties qu'on compte : le modèle retombe sur l'anatomie habituelle (une vache a 2 cornes) si on n'insiste pas
COUNTABLE = {"head": ("head", "heads"), "tail": ("tail", "tails"), "legs": ("leg", "legs"), "arms": ("arm", "arms"),
             "wings": ("wing", "wings"), "horns": ("horn", "horns"), "eyes": ("eye", "eyes"),
             "tentacles": ("tentacle", "tentacles"), "pincers": ("pincer", "pincers"), "ears": ("ear", "ears"),
             "fins": ("fin", "fins"), "fangs": ("fang", "fangs"), "hands": ("hand", "hands")}
SINGLE = {"horns": "a single horn growing from the middle of the forehead, like a unicorn's horn, not a pair of horns",
          "eyes": "a single eye in the middle of the face, like a cyclops, not a pair of eyes"}  # où va « la seule »
PAIRED = {"horns", "eyes", "wings", "ears", "arms", "legs", "pincers", "fangs", "hands", "fins"}  # « une corne » compte
ONE_RE = nb_core.re.compile(r"\b(?:un|une)\s+(?:seule?\s+)?(?:[a-z]+\s+)?(" + nb_core._PART_ALT + r")\b")


def counts(description):
    """{partie: nombre} demandés explicitement (« six tentacules », « une corne », « un seul œil »)."""
    spec = nb_core.analyze("", description)
    out = {k: n for k, n in spec.counts.items() if k in COUNTABLE}
    ones = {}
    for m in ONE_RE.finditer(nb_core.fold(description)):
        word = m.group(1)
        slot = nb_core.PARTS[word][0]
        if slot in PAIRED and not word.endswith(("s", "x")) and slot not in out:  # singulier seulement
            ones[slot] = ones.get(slot, 0) + 1  # « un œil rouge et un œil bleu » = 2
    return {**ones, **out}


def count_line(description):
    """Phrase affirmative (jamais « pas de corne sur les côtés » : nommer ce qu'on ne veut pas le fait dessiner)."""
    parts = []
    for slot, n in counts(description).items():
        one, many = COUNTABLE[slot]
        parts.append(f"exactly ONE {one} (1), " + SINGLE.get(slot, f"a single {one}") if n == 1
                     else f"exactly {nb_core.num_en(n)} {many} ({n})")
    if not parts:
        return ""
    return (" Count carefully: " + "; ".join(parts) + ". Draw exactly this number, "
            "as described, not the usual anatomy.")


# posture comprise (nb_writer.brief, ou devinée) : dite en clair, surtout au profil, où le modèle ne voit que la face
POSE = {
    "quatre_pattes": "It stands on all four legs.",
    "deux_jambes": "It stands upright on its two legs.",
    "plusieurs_pattes": "It stands on its {n} legs.",
    "flotte": ("It floats in mid-air, well above the ground, with empty space beneath its smooth rounded underside; "
               "it hovers, it does not stand."),
    "vole": "It hovers in mid-air on its wings, above the ground.",
    "rampe": "Its long body lies flat on the ground and slithers along it, belly on the ground.",
    "nage": "It swims, floating in the water with its fins, its body ending in a tail.",
    "sautille": "It crouches on its strong hind legs, ready to leap, like a frog or a hare.",
    "roule": "Its body is a compact ball, curled up round, ready to roll along the ground.",
    "glisse": "It is a soft, gooey mass resting on the ground, a low dome with no legs, that slides along.",
    "enracine": "It is rooted in the ground: its base spreads into roots or a stump planted in the soil, it does not walk.",
}
# au profil, le modèle ne voit que la face : on lui dit à quoi ressemble la silhouette de côté, posture par posture
PROFILE = {
    "quatre_pattes": (" Its body is horizontal: head at the left, rump and tail at the right, the four legs under the body "
                      "(the two near legs in front of the two far ones). The side view is wider than tall."),
    "deux_jambes": " It stands upright, seen from its side, one leg slightly ahead of the other, arms along its body.",
    "plusieurs_pattes": " Its body is horizontal, head at the left, and its legs are spread in a row along its side, all under the body.",
    "flotte": " It hovers well above the ground, with empty space under it, seen from the side.",
    "vole": " Its wings are spread and raised above its back, seen from the side, and it hovers above the ground.",
    "rampe": (" Its long body is stretched out flat along the ground, from its head at the left to the tip of its tail at the "
              "right, low and horizontal, slightly wavy: it does not stand up, rear up or coil upward. The side view is much "
              "wider than tall."),
    "nage": (" Its body is horizontal like a fish seen from the side: head at the left, tail fin at the right, fins along "
             "its belly and back. The side view is wider than tall."),
    "sautille": " It crouches, seen from the side, its big folded hind legs under its body, ready to leap toward the left.",
    "roule": " Seen from the side it is the same compact ball, round, sitting on the ground.",
    "glisse": " Seen from the side it is a low soft dome lying on the ground, wider than tall, sliding toward the left.",
    "enracine": " Seen from the side it is still rooted in the ground, its top leaning slightly toward the left.",
}
WIDE = {"quatre_pattes", "plusieurs_pattes", "rampe", "nage", "glisse"}  # profil plus large que haut, sinon mauvais profil
FLOATING = {"flotte", "vole", "nage"}
_GUESS = (("flotte", r"flott|levit|dans les airs|en l.air|plane\b|suspendu"), ("vole", r"\bvol(e|ant)\b"),
          ("rampe", r"\bramp(e|ant)|serpent|\bver\b|limace|anguille|mille.pattes"), ("nage", r"\bnage|poisson"),
          ("glisse", r"gelee|slime|\bblob|visqueu|glisse"), ("sautille", r"saute|sautill|bondi|grenouille|lievre|lapin|kangourou"),
          ("roule", r"\broule|en boule|boule roulante"), ("enracine", r"enracin|racines|souche|arbre vivant"))


LEGLESS = {"flotte", "rampe", "nage", "glisse", "roule"}


def said_posture(description):
    """La posture écrite en toutes lettres dans la description (« rampante », « qui flotte »), ou ""."""
    folded = nb_core.fold(description)
    return next((p for p, rx in _GUESS if nb_core.re.search(rx, folded)), "")


def fix_posture(description, brief):
    """(4) « une créature rampante à quatre pattes » : le mot « rampante » donnait la posture rampe et 0 patte, et le profil
    était dessiné à plat, couché. Les pattes comptées dans la description l'emportent (4 : à quatre pattes, 6 et plus :
    plusieurs pattes). Rend une copie de la fiche, corrigée ou non."""
    b = dict(brief or {})
    n, p = counts(description or "").get("legs", 0), b.get("posture")
    if n >= 4 and (p in ("rampe", "deux_jambes") or (p == "quatre_pattes" and n > 4)):
        b.update(posture="quatre_pattes" if n == 4 else "plusieurs_pattes", pattes=n)
    return b


def guess_brief(description):
    """Sans plume (pas d'Ollama) : la posture devinée par mots-clés, la description reste en français."""
    folded = nb_core.fold(description)
    posture = next((p for p, rx in _GUESS if nb_core.re.search(rx, folded)), "")
    if not posture and not nb_archetypes.says_posture(description):
        posture = next((p for p, _ in nb_archetypes.match(description) if p), "")
    legs = 0 if posture or not has_legs(description) else counts(description).get("legs", 0)  # 0 : on ne sait pas
    if legs and not posture:
        posture = {2: "deux_jambes", 4: "quatre_pattes"}.get(legs, "plusieurs_pattes")
    return dict(posture=posture, pattes=legs, en="", profil="")


def pose_line(brief):
    p = (brief or {}).get("posture")
    return " " + POSE[p].format(n=brief.get("pattes") or "many") if p in POSE else ""


def anatomy_line(description, brief=None):
    """Les nombres, placés : « 3 heads: each on its own neck… » (la plume) ou la phrase de comptage (mots-clés)."""
    parts = (brief or {}).get("anatomie")
    if parts:
        return " Exact anatomy, count carefully and draw every part: " + "; ".join(parts) + "."
    return count_line(description)


def reference_line(description):
    refs = nb_archetypes.match(description)
    return (" Reference: " + "; ".join(t for _, t in refs) + ".") if refs else ""


def describe(description, brief=None):
    """Ce qu'on dit du sujet au modèle d'image : l'anglais précis d'abord, le français d'origine ensuite."""
    b = brief or {}
    if b.get("en"):
        head = (b["silhouette"] + ". " if b.get("silhouette") else "") + b["en"]
        return (f"{head}{anatomy_line(description, b)}{reference_line(description)}{pose_line(b)}"
                f"\n(Original description, in French: {description.strip()})")
    return f"{description.strip()}{count_line(description)}{reference_line(description)}{pose_line(b)}"


def design_prompt(description, brief=None):
    return f"{describe(description, brief)}\n\n{STYLE} {FACING['bas']}"


# profil d'une créature à pattes : sans ça le corps se tasse (vache trapue, pattes cachées)
PROPORTIONS = (" Same proportions as the front view: same head size, same leg length and body size, drawn true to scale "
               "(the side view is as tall as the front view); all legs fully visible and straight under the body, "
               "feet on the ground.")


# (2, 3) un redessin après le contrôle : ce qui clochait, dit en plus au modèle
FIX_HINT = {
    "pattes": (" Every leg is drawn apart from the others, with clear empty space between the legs, mid-stride: the near "
               "front leg reaches forward and the near hind leg back, so each leg is seen on its own, down to its foot."),
    "tailles": " It is exactly as big and as tall as in the front view, same body length and proportions, not shrunk or stretched.",
    "couleurs": " It keeps exactly the colors of the front view: the same palette, markings and shades, nothing recolored.",
}


def view_prompt(direction, legs=False, brief=None, description=""):
    b = brief or {}
    head = f"The creature: {describe(description, b)}\n" if (b.get("en") or description.strip()) else ""
    side = f" Seen from the side: {b['profil']}" if direction == "gauche" and b.get("profil") else ""
    side += PROFILE.get(b.get("posture"), "") if direction == "gauche" else ""
    known = b.get("posture") in POSE
    facing = FACING[direction].replace("{posture}", "" if known else GUESSED_POSTURE)
    walks = legs or not known  # posture inconnue : on garde les pattes dans la liste, comme avant
    keep = KEEP.format(parts="heads, " + ("legs, " if walks else "") + "eyes, horns, tentacles and wings")
    return head + facing + side + (PROPORTIONS if legs and direction == "gauche" else "") + keep


def _loaders(g, model):
    cfg = MODELS[model]
    g["1"] = {"class_type": "UNETLoader", "inputs": {"unet_name": cfg["unet"], "weight_dtype": "default"}}
    g["2"] = {"class_type": "CLIPLoader", "inputs": {"clip_name": ENCODER, "type": cfg["clip_type"], "device": "default"}}
    g["3"] = {"class_type": "VAELoader", "inputs": {"vae_name": cfg["vae"]}}
    return ["1", 0], ["2", 0], ["3", 0]


def build_graph(model, prompt, seed, *, refs=(), size=SIZE, steps=None, cut=False, prefix="neobestiary/engine",
                init=None, mask=None):
    """Graphe texte -> image. refs : noms d'images déjà envoyées à ComfyUI (édition par référence, klein seulement).
    cut : détoure la créature avec BiRefNet et sauve un PNG transparent.
    init + mask (klein) : retouche ciblée, seule la zone du masque est redessinée (le masque est l'alpha inversé
    de l'image envoyée : transparent = à redessiner), puis l'original est recollé autour au pixel près."""
    cfg = MODELS[model]
    steps = steps or cfg["steps"]
    g = {}
    unet, clip, vae = _loaders(g, model)
    g["4"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": clip, "text": prompt}}
    g["5"] = {"class_type": "CLIPTextEncode", "inputs": {"clip": clip, "text": ""}}
    pos, neg = ["4", 0], ["5", 0]
    if model == "klein":
        for i, name in enumerate(refs):
            li, ls, le, lr = (str(20 + i * 4 + k) for k in range(4))
            g[li] = {"class_type": "LoadImage", "inputs": {"image": name}}
            g[ls] = {"class_type": "ImageScaleToTotalPixels",
                     "inputs": {"image": [li, 0], "upscale_method": "nearest-exact", "megapixels": 1.0, "resolution_steps": 1}}
            g[le] = {"class_type": "VAEEncode", "inputs": {"pixels": [ls, 0], "vae": vae}}
            g[lr] = {"class_type": "ReferenceLatent", "inputs": {"conditioning": pos, "latent": [le, 0]}}
            pos = [lr, 0]
        g["6"] = {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": size, "height": size, "batch_size": 1}}
        g["7"] = {"class_type": "Flux2Scheduler", "inputs": {"steps": steps, "width": size, "height": size}}
        g["8"] = {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}}
        g["9"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": int(seed)}}
        g["10"] = {"class_type": "CFGGuider", "inputs": {"model": unet, "positive": pos, "negative": neg, "cfg": 1.0}}
        latent = ["6", 0]
        if init and mask:
            g["30"] = {"class_type": "LoadImage", "inputs": {"image": init}}
            g["31"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["30", 0], "vae": vae}}
            g["32"] = {"class_type": "LoadImage", "inputs": {"image": mask}}
            g["33"] = {"class_type": "SetLatentNoiseMask", "inputs": {"samples": ["31", 0], "mask": ["32", 1]}}
            latent = ["33", 0]
        g["11"] = {"class_type": "SamplerCustomAdvanced",
                   "inputs": {"noise": ["9", 0], "guider": ["10", 0], "sampler": ["8", 0], "sigmas": ["7", 0],
                              "latent_image": latent}}
        samples = ["11", 0]
    else:
        if refs:
            raise ValueError("Z-Image Turbo ne fait pas d'édition par référence")
        g["6"] = {"class_type": "EmptySD3LatentImage", "inputs": {"width": size, "height": size, "batch_size": 1}}
        g["7"] = {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": unet, "shift": 3.0}}
        g["11"] = {"class_type": "KSampler",
                   "inputs": {"model": ["7", 0], "positive": pos, "negative": neg, "latent_image": ["6", 0],
                              "seed": int(seed), "steps": steps, "cfg": 1.0, "sampler_name": "euler",
                              "scheduler": "simple", "denoise": 1.0}}
        samples = ["11", 0]
    g["12"] = {"class_type": "VAEDecode", "inputs": {"samples": samples, "vae": vae}}
    image = ["12", 0]
    if init and mask and model == "klein":  # hors de la zone : les pixels d'origine, exactement
        g["34"] = {"class_type": "ImageCompositeMasked",
                   "inputs": {"destination": ["30", 0], "source": image, "x": 0, "y": 0, "resize_source": False,
                              "mask": ["32", 1]}}
        image = ["34", 0]
    if cut:
        g["14"] = {"class_type": "LoadBackgroundRemovalModel", "inputs": {"bg_removal_name": CUT_MODEL}}
        g["15"] = {"class_type": "RemoveBackground", "inputs": {"bg_removal_model": ["14", 0], "image": image}}
        g["16"] = {"class_type": "InvertMask", "inputs": {"mask": ["15", 0]}}  # JoinImageWithAlpha ré-inverse
        g["17"] = {"class_type": "JoinImageWithAlpha", "inputs": {"image": image, "alpha": ["16", 0]}}
        image = ["17", 0]
    g["13"] = {"class_type": "SaveImage", "inputs": {"images": image, "filename_prefix": prefix}}
    return g


def ready(client, model):
    """Liste des fichiers manquants pour ce modèle (vide = prêt)."""
    cfg = MODELS[model]
    have_unet = client.choices("UNETLoader", "unet_name")
    have_clip = client.choices("CLIPLoader", "clip_name")
    have_vae = client.choices("VAELoader", "vae_name")
    have_cut = client.choices("LoadBackgroundRemovalModel", "bg_removal_name")
    return [f for f, ok in ((cfg["unet"], cfg["unet"] in have_unet), (ENCODER, ENCODER in have_clip),
                            (cfg["vae"], cfg["vae"] in have_vae), (CUT_MODEL, CUT_MODEL in have_cut)) if not ok]


def _flat(im):
    """Référence envoyée au modèle : créature détourée posée sur un gris uni (sans l'ombre d'origine)."""
    if im.mode != "RGBA":
        return im.convert("RGB")
    bg = Image.new("RGB", im.size, (200, 200, 200))
    bg.paste(im, mask=im.getchannel("A"))
    return bg


def clean_cut(im):
    """Finit le détourage de BiRefNet : bord net, plus de liseré du fond gris, plus de miettes détachées (restes du
    fond, étincelles). Les trous ne sont pas bouchés : un vide entre l'aile et la queue est souvent un vrai vide."""
    import numpy as np
    from scipy import ndimage
    im = im.convert("RGBA")
    arr = np.asarray(im).astype(np.float32)
    rgb, a = arr[..., :3].copy(), arr[..., 3].copy()
    if (a >= 200).sum() < 50:
        return im
    clear = a < 16
    bg = np.median(rgb[clear], axis=0) if clear.sum() > 100 else np.array([200., 200., 200.])
    a[a < 48] = 0
    body = a > 0
    # liseré : pixels du bord (2 px) de la couleur du fond
    rim = body & ~ndimage.binary_erosion(body, iterations=2)
    a[rim & (np.abs(rgb - bg).max(axis=2) < 14)] = 0
    body = a > 0
    # miettes : tout morceau détaché minuscule, ou un petit morceau gris clair comme le fond
    lab, n = ndimage.label(body)
    if n > 1:
        sizes = ndimage.sum(body, lab, range(1, n + 1))
        big = sizes.max()
        for i, size in enumerate(sizes, 1):
            if size >= big:
                continue
            part = lab == i
            col = rgb[part].mean(axis=0)
            if size < big * .004 or (size < big * .05 and np.abs(col - bg).max() < 30):
                a[part] = 0
        body = a > 0
    a[a >= 230] = 255
    # bord : les pixels semi-transparents prennent la couleur de l'intérieur voisin, pas celle du fond
    solid = (a == 255).astype(np.float32)
    edge = (a > 0) & (a < 255) & ~ndimage.binary_erosion(a > 0, iterations=3)  # le bord seulement : ailes translucides gardées
    w = ndimage.gaussian_filter(solid, 2)
    c = np.stack([ndimage.gaussian_filter(rgb[..., k] * solid, 2) for k in range(3)], axis=2)
    fix = edge & (w > .05)
    rgb[fix] = c[fix] / w[fix][:, None]
    out = np.dstack([rgb, a]).clip(0, 255).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def render(client, model, prompt, seed, refs_img=(), cancel=lambda: False, **kw):
    refs = [client.upload_png(_flat(im), f"eref_{uuid.uuid4().hex[:8]}.png") for im in refs_img]
    g = build_graph(model, prompt, seed, refs=refs, **kw)
    im = nbc.run_workflow(client, g, cancel, mode="RGBA" if kw.get("cut") else "RGB")
    return clean_cut(im) if kw.get("cut") else im


# « supprime les cornes », « sans queue » : le modèle d'édition ajoute souvent au lieu d'enlever ; on lui dit clairement d'effacer
_REMOVE = (r"^\s*(?:supprime[rz]?|enleve[rz]?|retire[rz]?|efface[rz]?|ote[rz]?|vire[rz]?|elimine[rz]?|fais disparaitre|sans|plus de|pas de)"
           r"(?:-(?:le|la|les|lui))?\s+(.+)$")


def removal(instruction):
    """Ce qu'une retouche demande d'enlever (« supprime les cornes » -> « les cornes »), ou None."""
    m = nb_core.re.match(_REMOVE, nb_core.fold(instruction))
    return instruction.strip()[m.start(1):].strip(" .!") if m else None


def edit_prompt(instruction, zone):
    where = "Edit only the marked area of this creature" if zone else "Edit this creature"
    gone = removal(instruction)
    if gone:
        return (f"{where}: REMOVE this part completely: \"{gone}\". Erase it and fill the place where it was with the creature's "
                "own body or the plain background, as if it had never existed. Do not add anything new. Keep everything else exactly "
                "identical: same design, colors, proportions, pose, camera and pixel art style. Plain flat light gray background, "
                "no cast shadow.")
    return (f"{where}: {instruction.strip()}. Keep everything else exactly identical: same design, colors, "
            "proportions, pose, camera and pixel art style. Plain flat light gray background, no cast shadow.")


def zone_mask(size, x, y, radius):
    """Masque de retouche : un disque adouci autour du point cliqué (x, y, radius en fraction de l'image).
    Envoyé comme alpha inversé : LoadImage de ComfyUI donne masque = 1 - alpha."""
    from PIL import ImageDraw, ImageFilter
    w, h = size
    m = Image.new("L", size, 0)
    r = max(4, radius * w)
    ImageDraw.Draw(m).ellipse((x * w - r, y * h - r, x * w + r, y * h + r), fill=255)
    m = m.filter(ImageFilter.GaussianBlur(r * .15))
    out = Image.new("RGBA", size, (0, 0, 0, 255))
    out.putalpha(m.point(lambda v: 255 - v))
    return out


# --- corrections d'orientation : le modèle d'édition ne sait pas faire tourner une créature (« il est de dos, il doit
# être de face » ne fait que repeindre). On reconnaît ces phrases et on agit nous-mêmes : miroir, retournement, ou la vue
# refaite dans la bonne orientation, avec une consigne renforcée.
_O = r"(?:vue? |vu |tourne(?:e)? )?de (face|dos|profil)"
_FLIP = (r"\b(?:la )?tete en bas\b|\bsens dessus dessous\b|\b(?:les )?pattes en l'air\b|\bsur le dos\b"
         r"|\ba l'envers (?:de haut en bas|verticalement)\b|\bretourne(?:e)? (?:de haut en bas|verticalement)\b")
_MIRROR = (r"\bregarde (?:vers |a )?(?:la )?droite\b|\btourne(?:e)? vers la droite\b|\bla tete (?:a|vers la) droite\b"
           r"|\ben miroir\b|\bde l'autre cote\b|\bmauvais cote\b")
# une demande de sens (gauche / droite) : un simple miroir, quelle que soit la vue, jamais une vue redessinée
_HMIRROR = (r"\b(?:change[rz]? de|dans l'autre|l'autre|inverse[rz]? (?:la|le|sa|son)?) ?(?:sens|direction)\b|\bhorizontal(?:ement)?\b"
            r"|\bsymetri\w*\b|\bmiroir\b|\bregard\w* (?:vers |a )?(?:la )?gauche\b|\btourne\w* vers la gauche\b|\bla tete (?:a|vers la) gauche\b")
_ENVERS = r"\ba l'envers\b|\bretourne(?:e|-le|-la)?\b|\bdans le mauvais sens\b|\bdans l'autre sens\b|\binverse(?:e)?\b|\bmal oriente(?:e)?\b"
_WANT = (r"(?:^|\bmais|\bdoit etre|\bdevrait etre|\bil faut (?:qu'(?:il|elle) soit|le mettre|la mettre)?|\bje (?:le |la )?veux"
         r"|\bremets?(?:-le|-la| le| la)?|\bmets?(?:-le|-la| le| la)?|\bplutot|\bpas de \w+ mais)\s*" + _O)
_NOT = r"(?:\bn'est pas|\best pas|\bpas|\bne doit pas etre|\bplus)\s+" + _O
_SEEN = r"\bon (?:voit|apercoit|distingue) (?:bien |encore )?(?:son|le|ses|sa|les) (dos|visage|yeux|figure|face|museau|bouche)\b"
_IS = r"\b(?:il|elle) est " + _O
EXPECTED = {"bas": "face", "haut": "dos", "gauche": "profil"}
FIX_NOTE = {"mirror": "retournée en miroir", "flip": "remise à l'endroit", "bas": "remise de face", "haut": "remise de dos",
            "gauche": "remise de profil"}


def orientation_fix(instruction, key):
    """Ce qu'une retouche demande pour l'orientation de la vue key (bas = face, haut = dos, gauche = profil tête à gauche).
    Rend (action, reste) : action = "mirror" | "flip" | "regen" | None ; reste = la retouche ordinaire qui reste à faire."""
    re = nb_core.re
    t = nb_core.fold(instruction)
    spans, wrong, want = [], set(), None

    def grab(pat):
        found = list(re.finditer(pat, t))
        spans.extend(m.span() for m in found)
        return found
    flip = bool(grab(_FLIP))
    mirror = bool(grab(_MIRROR))
    hmirror = bool(grab(_HMIRROR))
    for m in grab(_WANT):
        want = m.group(1)
    for m in grab(_NOT):
        wrong.add(m.group(1))
    for m in grab(_SEEN):
        wrong.add("dos" if m.group(1) == "dos" else "face")
    for m in grab(_IS):
        if m.group(1) != EXPECTED.get(key):
            wrong.add(m.group(1))
    envers = bool(grab(_ENVERS))
    wrong.discard(want)
    exp = EXPECTED.get(key)
    if flip:
        action = "flip"
    elif hmirror or (mirror and key != "gauche" and not wrong):
        action = "mirror"
    elif key == "gauche":
        if wrong & {"face", "dos"} or (want == "profil" and not mirror):
            action = "regen"
        elif mirror or envers:
            action = "mirror"
        else:
            action = None
    elif key in ("bas", "haut"):
        action = "regen" if (envers or wrong or want == exp or (want and want != exp)) else None
    else:
        action = None
    if action is None:
        return None, instruction.strip()
    keep = [c for i, c in enumerate(t) if not any(a <= i < b for a, b in spans)]
    rest = re.sub(r"\s+", " ", "".join(keep))
    rest = re.sub(r"^(?:[\s,.;:!-]|\b(?:il|elle|est|mais|et|c'est|non|pas|alors|donc|stp|svp)\b)+", "", rest)
    rest = re.sub(r"(?:[\s,.;:!-]|\b(?:il|elle|est|mais|et|pas)\b)+$", "", rest)
    return action, (rest if len(re.findall(r"[a-z]{3,}", rest)) >= 2 else "")


def generate_facing(client, direction, refs, seed, progress=lambda m: None, cancel=lambda: False, legs=False, brief=None,
                    description=""):
    """La vue refaite dans la bonne orientation : consigne d'orientation renforcée, 2 essais, le mieux noté gardé."""
    insist = {"bas": " The previous picture wrongly showed it from behind or from the side: this time it must face the "
                     "viewer, its face and eyes clearly visible, toward the bottom of the screen.",
              "haut": " The previous picture wrongly showed its face: this time we must see only its back and the back of "
                      "its head, with no eyes and no face visible.",
              "gauche": " The previous picture was not a clean side view: this time show a pure side profile, its head "
                        "pointing to the left edge of the screen."}[direction]
    prompt = (STYLE + " " if direction == "bas" else "") + view_prompt(direction, legs, brief, description) + insist
    return _best(client, prompt, refs, direction, seed, 2, cancel, progress, "Orientation",
                 (brief or {}).get("posture", ""))[0]


def generate_edit(client, image, instruction, seed, zone=None, progress=lambda m: None, cancel=lambda: False):
    """Retouche : zone = (x, y, rayon) en fractions -> seule cette zone est redessinée ; None -> toute la créature,
    éditée d'après elle-même (« les yeux en rouge »)."""
    progress("Retouche")
    flat = _flat(image.convert("RGBA")).resize((SIZE, SIZE), Image.Resampling.LANCZOS)
    ref = client.upload_png(flat, f"edit_{uuid.uuid4().hex[:8]}.png")
    kw = {}
    if zone:
        kw = dict(init=ref, mask=client.upload_png(zone_mask(flat.size, *zone), f"mask_{uuid.uuid4().hex[:8]}.png"))
    g = build_graph(MODEL, edit_prompt(instruction, bool(zone)), seed, refs=[ref], cut=True, **kw)
    return nbc.run_workflow(client, g, cancel, mode="RGBA")


GENRE_EN = {"griffe": "a slashing claw strike", "morsure": "a lunging bite, jaws wide open", "charge": "a headlong charge",
            "projectile": "spitting a projectile", "souffle": "a breath attack streaming from its mouth",
            "onde": "a shockwave bursting around it", "rayon": "firing an energy beam"}
ELEMENT_EN = {"feu": "fire", "eau": "water", "plante": "plant", "vent": "wind", "roche": "rock", "glace": "ice",
              "foudre": "lightning", "lumière": "light", "ombre": "shadow", "esprit": "spirit", "acier": "steel",
              "poison": "poison", "insecte": "insect swarm", "fée": "fairy sparkles", "son": "sound waves",
              "temps": "golden clockwork and swirling hourglass sand", "encre": "black ink splashes and calligraphy strokes",
              "gravité": "purple gravity distortion with orbiting rocks"}


def portrait_prompt(move, brief=None, description=""):
    what = GENRE_EN.get(move.get("genre"), "attacking")
    elem = ELEMENT_EN.get(move.get("element"), "")
    return (f"The same creature in a dynamic battle pose, mid-attack: {what}, with {elem} energy. "
            "Dramatic three-quarter front view, large, filling the frame, the entire creature visible. "
            "Keep the identical design, colors, anatomy (same number of heads, eyes, legs, horns) and pixel art style; "
            "every head and body part stays clearly visible, side by side, none hidden behind the body. "
            f"The creature: {describe(description, brief)} "
            "Plain flat light gray background, no text, no scenery, no cast shadow.")


def generate_portrait(client, design, move, seed, progress=lambda m: None, cancel=lambda: False, brief=None,
                      description=""):
    """Le portrait de combat : la créature en pleine attaque (sa 2e attaque, la plus forte), d'après son design."""
    progress("Portrait de combat")
    return render(client, MODEL, portrait_prompt(move, brief, description), seed, refs_img=[design], cancel=cancel, cut=True)


def illustration_prompt(brief=None, description=""):
    return ("A bestiary illustration of this exact creature, like a plate in a naturalist's field guide: the creature "
            "calm, in its natural and most characteristic posture, seen at a three-quarter angle, its whole body and every "
            "part clearly visible and readable, nothing hidden or cropped, large and centered. "
            "Keep the identical design, colors, anatomy (same number of heads, eyes, legs, wings, horns, tentacles) and "
            f"pixel art style. The creature: {describe(description, brief)} "
            "Detailed pixel art, plain flat light gray background, no text, no scenery, no cast shadow.")


def generate_illustration(client, design, seed, progress=lambda m: None, cancel=lambda: False, brief=None, description=""):
    """L'image du bestiaire : la créature au calme, entière et fidèle à sa description, d'après son design."""
    progress("Image du bestiaire")
    return render(client, MODEL, illustration_prompt(brief, description), seed, refs_img=[design], cancel=cancel, cut=True)


def fusion_prompt(description, brief=None):
    return ("Combine the two reference creatures into ONE single new hybrid creature that clearly mixes visible traits "
            f"of both. {STYLE} The hybrid: {describe(description, brief)} {pose_line(brief)}")


def generate_fusion(client, designs, description, seed, progress=lambda m: None, cancel=lambda: False, brief=None):
    progress("Esquisse de l'hybride")
    return render(client, MODEL, fusion_prompt(description, brief), seed, refs_img=list(designs), cancel=cancel, cut=True)


# --------------------------------------------------------------------------- créature complète

MODEL = "klein"
DIRECTIONS = ("bas", "gauche", "droite", "haut")
TRIES = 2  # candidats par vue ; le meilleur est gardé, les autres restent comme versions


def _colors(im):
    """Histogramme 512 cases des pixels opaques (pour comparer les couleurs de deux vues)."""
    small = im.convert("RGBA").resize((96, 96), Image.Resampling.NEAREST)
    h = [0] * 512
    for r, g, b, a in small.getdata():
        if a >= 128:
            h[(r >> 5) << 6 | (g >> 5) << 3 | b >> 5] += 1
    n = sum(h) or 1
    return [v / n for v in h]


def similarity(a, b):
    """0..1 : part de couleurs communes entre deux images détourées."""
    return sum(min(x, y) for x, y in zip(_colors(a), _colors(b)))


def score_view(im, ref, direction):
    """Note d'un candidat : silhouette propre (symétrique de face/dos) + mêmes couleurs que le design."""
    view = {"bas": "face", "haut": "dos"}.get(direction, direction)
    base, _ = nb_auto.score_image(im.convert("RGB"), view)
    return base + 6.0 * similarity(im, ref)


def crawls_on_legs(description, brief=None):
    """« un insecte à six pattes qui rampe » : la fiche dit « rampe, 0 patte », mais il marche sur ses pattes."""
    return (brief or {}).get("posture") == "rampe" and counts(description or "").get("legs", 0) >= 2


def has_legs(description, brief=None):
    """La marche par édition de pose n'a de sens qu'avec des pattes ; sinon (œil, blob, serpent) : rebond."""
    if crawls_on_legs(description, brief):
        return True
    if brief and brief.get("posture"):
        return brief["posture"] not in FLOATING | {"rampe", "glisse", "roule"} and brief.get("pattes", 0) > 0
    spec = nb_core.analyze("", description)
    if spec.only or "legs" in spec.forbidden or "body" in spec.forbidden:
        return False
    return nb_guides.plan_for(spec) not in ("blob", "serpent")  # inconnu (« chevalier ») : il marche


def bob(im, phase):
    """Rebond procédural : phase -1/+1 = la créature se tasse un peu et descend, ±0.5 = à moitié, 0 = repos."""
    if not phase:
        return im.copy()
    w, h = im.size
    box = im.getbbox() or (0, 0, w, h)
    ch = box[3] - box[1]
    squash = max(1, round(ch * 0.04 * abs(phase)))
    body = im.crop(box).resize((box[2] - box[0], ch - squash), Image.Resampling.NEAREST)
    out = Image.new("RGBA", im.size, (0, 0, 0, 0))
    out.paste(body, (box[0], box[1] + squash))
    return out


def has_wings(description, brief=None):
    text = " ".join([description, (brief or {}).get("en", "")] + list((brief or {}).get("anatomie", []))).lower()
    return bool(re.search(r"\b(wing(s|ed)?|ailes?|ail[ée]e?s?)\b", text))  # mots entiers : « glowing » contient « wing »


def flap(im, direction, phase):
    """Battement d'ailes : les colonnes hors du tronc montent (phase +1) ou descendent (-1), d'autant plus qu'elles
    sont loin du tronc. Face / dos : les deux côtés ; profil (tête à gauche) : seulement l'arrière, où sont les ailes."""
    im = im.convert("RGBA")
    box = im.getbbox()
    if not box or not phase:
        return im.copy()
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    mid = (x0 + x1) / 2
    # shortcut: tronc = 36 % central de la largeur (face) ou moitié avant (profil), sans détecter les ailes
    body, reach = (w * .18, w / 2 - w * .18) if direction in ("bas", "haut") else (w * .1, w / 2 - w * .1)
    amp = max(2, round(h * .07))
    out = Image.new("RGBA", im.size, (0, 0, 0, 0))
    for x in range(x0, x1):
        d = (abs(x + .5 - mid) if direction in ("bas", "haut") else x + .5 - mid) - body
        col = im.crop((x, 0, x + 1, im.height))
        out.alpha_composite(col, (x, 0)) if d <= 0 else out.paste(col, (x, -round(phase * amp * d / reach)), col)
    return out


def shape_penalty(im, direction, posture):
    """Un serpent ou un quadrupède de profil est plus large que haut : un profil dressé est un raté."""
    box = im.getbbox()
    if direction != "gauche" or posture not in WIDE or not box:
        return 0.0
    w, h = box[2] - box[0], box[3] - box[1]
    return 0.0 if w >= h * 1.1 else 8.0


def _best(client, prompt, refs, direction, seed, tries, cancel, progress, label, posture=""):
    """refs[0] = le design (sert aussi à noter les couleurs)."""
    ref = refs[0]
    cands = []
    for i in range(tries):
        if cancel():
            raise nbc.ComfyError("annulé")
        progress(f"{label} ({i + 1}/{tries})")
        im = render(client, MODEL, prompt, seed + 1000 * i, refs_img=refs, cancel=cancel, cut=True)
        cands.append((score_view(im, ref, direction) - shape_penalty(im, direction, posture), im))
    cands.sort(key=lambda c: -c[0])
    return [im for _, im in cands]


def generate_design(client, description, seed, progress=lambda m: None, cancel=lambda: False, brief=None):
    progress("Esquisse du design")
    return render(client, MODEL, design_prompt(description, brief), seed, cancel=cancel, cut=True)


def generate_directions(client, design, seed, progress=lambda m: None, cancel=lambda: False, tries=TRIES, legs=False,
                        brief=None, description=""):
    """{direction: [versions, la meilleure d'abord]} ; droite = miroir de gauche (même créature, garanti)."""
    out = {"bas": [design]}
    out["haut"] = _best(client, view_prompt("haut", False, brief, description), [ImageOps.mirror(design)], "haut", seed + 7,
                        tries, cancel, progress, "Vue de dos")
    # à pattes : le design seul (le dos en référence laisse sa raie sur le flanc et tasse le corps) ;
    # sans pattes (œil, pieuvre) : face + dos, sinon le modèle invente un corps à quatre pattes en tournant de 90°
    refs = [design] if legs else [design, out["haut"][0]]
    out["gauche"] = _best(client, view_prompt("gauche", legs, brief, description), refs, "gauche", seed + 7, tries, cancel,
                          progress, "Profil", (brief or {}).get("posture", ""))
    out["droite"] = [ImageOps.mirror(im) for im in out["gauche"]]
    return out


def generate_view(client, refs, direction, seed, progress=lambda m: None, cancel=lambda: False, legs=False,
                  brief=None, description="", fix=()):
    """Une nouvelle version d'une vue (« Refaire ») : refs = [design] ou [design, dos] pour le profil.
    fix : ce que le contrôle a trouvé (pattes, tailles, couleurs), redit au modèle (FIX_HINT)."""
    label = {"haut": "Vue de dos", "gauche": "Profil"}[direction]
    if legs:
        refs = refs[:1]
    if direction == "haut":  # vu de dos, la gauche et la droite s'échangent : le bâton tenu à gauche passe à droite
        refs = [ImageOps.mirror(refs[0])]
    prompt = view_prompt(direction, legs, brief, description) + "".join(FIX_HINT[k] for k in fix if k in FIX_HINT)
    return _best(client, prompt, refs, direction, seed, 1, cancel, progress, label, (brief or {}).get("posture", ""))[0]


def leg_zone(im):
    """Hauteur (en px, depuis le haut de l'image) où commencent les pattes : sous le corps, la silhouette se sépare
    en plusieurs colonnes ou devient nettement plus étroite. Bornée entre 15 % et 45 % du bas de la créature."""
    a = im.getchannel("A").point(lambda v: 255 if v >= 128 else 0)
    box = a.getbbox()
    if not box:
        return None, None
    x0, y0, x1, y1 = box
    h = y1 - y0
    px = np.asarray(a) > 0

    def runs(y):
        row = px[y, x0:x1]
        return int(row[0]) + int((row[1:] & ~row[:-1]).sum()), int(row.sum())

    widest = max(runs(y)[1] for y in range(y0, y1, max(1, h // 40)))
    top = y1 - round(h * .15)
    # on remonte tant que ça ressemble à des pattes ; quelques lignes où elles se croisent (deux pattes l'une devant
    # l'autre près du sol) ne l'arrêtent plus, si au moins 3 lignes de pattes reprennent au-dessus : avant, seuls les
    # pieds bougeaient, et ils glissaient en se détachant
    gap, run, pending = 0, 0, False
    for y in range(y1 - 1, y1 - round(h * .5), -1):
        n, w = runs(y)
        if n >= 2 or w < widest * .55:
            run, gap = run + 1, 0
            pending = pending and run < 3
            if not pending and y <= y1 - round(h * .15):
                top = y
        else:
            gap, run, pending = gap + 1, 0, True
            if y < y1 - round(h * .15) and gap > max(2, round(h * .1)):
                break
    return top, box


def _leg_masks(im, top):
    """Chaque patte à part : les morceaux de silhouette sous `top`, de gauche à droite. Les miettes (un orteil détaché
    par la découpe) suivent la patte la plus proche : elles ne s'envolent plus seules. [] si on ne trouve pas 2 à 10 pattes."""
    a = np.asarray(im.getchannel("A")) >= 128
    zone = a.copy()
    zone[:top] = False
    lab, n = ndimage.label(zone, structure=np.ones((3, 3)))
    if n < 2:
        return []
    sizes = ndimage.sum(zone, lab, range(1, n + 1))
    big = [k + 1 for k, v in enumerate(sizes) if v >= max(6, zone.sum() * .05)]
    if not 2 <= len(big) <= 10:
        return []
    cx = {k: np.nonzero(lab == k)[1].mean() for k in range(1, n + 1)}
    groups = {b: lab == b for b in big}
    for k in range(1, n + 1):
        if k not in groups:
            groups[min(big, key=lambda b: abs(cx[b] - cx[k]))] |= lab == k
    return [groups[b] for b in sorted(big, key=lambda b: cx[b])]


def _feet_masks(im, top, box, ground=False):
    """Chaque patte suivie depuis son pied : les pieds sont les morceaux séparés tout en bas, puis on remonte ligne par ligne
    vers la hanche ; un morceau qui réunit plusieurs pattes (elles se touchent en haut) est partagé au plus près de chacune,
    un morceau large qui ne porte qu'un pied (le ventre) reste au corps. [] si on ne trouve pas au moins 2 pieds."""
    a = np.asarray(im.getchannel("A")) >= 128
    x0, _, x1, y1 = box

    def runs(y):
        d = np.diff(np.concatenate([[0], a[y, x0:x1].astype(np.int8), [0]]))
        return [(x0 + int(s), x0 + int(e) - 1) for s, e in zip(np.nonzero(d == 1)[0], np.nonzero(d == -1)[0])]
    gap = max(2, round((x1 - x0) * .03)) if ground else 0  # de profil, deux pattes proches restent deux pieds

    def feet_at(y):  # les griffes d'un même pied (séparées de quelques px) ne comptent que pour un pied
        out = []
        for r in runs(y):
            if out and gap and r[0] - out[-1][1] - 1 <= gap:
                out[-1] = (out[-1][0], r[1])
            else:
                out.append(r)
        return out
    band = range(y1 - 1, top + (y1 - top) // 3 - 1, -1)  # les deux tiers bas de la zone : des pattes écartées ont leurs pieds plus haut
    best = max(band, key=lambda y: (len(feet_at(y)), y))  # la ligne où l'on voit le plus de pieds (la plus basse à égalité)
    feet = feet_at(best)
    if ground:  # de face : un pied touche le sol (le menton d'une tête baissée entre les pattes, non)
        low = y1 - 1 - max(2, round((y1 - top) * .08))
        feet = [(r0, r1) for r0, r1 in feet if a[low:y1, r0:r1 + 1].any()]
    if not 2 <= len(feet) <= 10:
        return []
    cx = [(r0 + r1) / 2 for r0, r1 in feet]
    wf = [r1 - r0 + 1 for r0, r1 in feet]
    masks = [np.zeros_like(a) for _ in feet]
    for y in range(y1 - 1, top - 1, -1):
        got = [[] for _ in feet]
        for r0, r1 in runs(y):
            mine = [k for k, c in enumerate(cx) if r0 - 2 <= c <= r1 + 2]
            if not mine:  # rien au-dessus d'un pied : le corps
                continue
            xs = np.arange(r0, r1 + 1)
            c = np.array([cx[k] for k in mine])
            who = np.array(mine)[np.abs(xs[:, None] - c[None]).argmin(1)]  # chaque pixel va au pied le plus proche
            wk, ck = np.array(wf)[who], np.array(cx)[who]
            # plusieurs pieds sous un même morceau : il est partagé entre eux, sauf s'il est bien plus large qu'eux (la tête
            # baissée entre les pattes de devant, le poitrail) : alors seul le haut de chaque patte suit, le reste est au corps
            shared = len(mine) > 1 and r1 - r0 + 1 <= 1.6 * sum(wf[k] for k in mine) + 2 * gap
            ok = shared | (r1 - r0 + 1 <= 2.5 * wk + 2) | (np.abs(xs - ck) <= wk * .75 + 1)
            for k in mine:
                got[k] += xs[ok & (who == k)].tolist()
        for k, xs in enumerate(got):
            if xs:
                masks[k][y, xs] = True
                cx[k] = sum(xs) / len(xs)  # la patte peut pencher : son centre suit
    return [m for m in masks if m.sum() >= 4]


def _halves(im, top, box):
    """Repli quand les pattes se touchent : deux moitiés, coupées sur la colonne la plus vide près du milieu
    (et non pile au milieu, qui tranchait un pied en deux)."""
    a = np.asarray(im.getchannel("A")) >= 128
    x0, _, x1, y1 = box
    mid, r = (x0 + x1) // 2, max(1, (x1 - x0) // 6)
    cut = min(range(mid - r, mid + r + 1), key=lambda x: (a[top:y1, x].sum(), abs(x - mid)))
    left = np.zeros_like(a)
    left[top:, :cut] = a[top:, :cut]
    right = np.zeros_like(a)
    right[top:, cut:] = a[top:, cut:]
    return [m for m in (left, right) if m.any()]


def step_frame(im, direction, phase, g=None):
    """Un pas procédural à partir de la pose de repos : mêmes pixels, seules les pattes bougent, chacune à part.

    Les pattes voisines sont en opposition (droite/gauche, avant/arrière : la marche en diagonale des quadrupèdes).
    profil (tête à gauche) : chaque patte pivote depuis la hanche (cisaillement nul en haut, fort au pied) ; celle qui
    passe devant lève le pied au demi-pas. face / dos : la patte qui avance se lève et se raccourcit, l'autre porte.
    Au pas complet, le corps descend d'un pixel (appui).
    g : la démarche (gait) ; le haut du corps suit le pas (_upper : queue, tête, oreilles, têtes, ailes).
    """
    g = g or GAIT[""]
    im = im.convert("RGBA")
    top, box = leg_zone(im)
    if top is None:
        return im.copy()
    x0, y0, x1, y1 = box
    h, zh = y1 - y0, max(1, y1 - top)
    # les pattes : par morceaux séparés, ou suivies depuis les pieds quand elles se touchent (sinon deux moitiés bougeaient
    # chacune d'un bloc : les deux pattes avant ensemble, les deux arrière ensemble)
    face = direction in ("bas", "haut")  # de face, les morceaux nets d'abord : le suivi depuis les pieds y déchirait les pattes
    lm, fm = _leg_masks(im, top), _feet_masks(im, top, box, ground=face)
    # un morceau du milieu bien plus large que les autres est le ventre, pas une patte : il reste en place (sinon il passait
    # pour une patte du milieu et les deux vraies pattes, de chaque côté, se levaient ensemble)
    # (on y cherche alors les pieds : deux pattes collées sous le ventre sont suivies chacune depuis son pied)
    if len(lm) >= 3:
        wid = [int(np.ptp(np.nonzero(m)[1])) + 1 for m in lm]
        keep = []
        for k, m in enumerate(lm):
            if 0 < k < len(lm) - 1 and wid[k] > 1.8 * np.median([w for j, w in enumerate(wid) if j != k]):
                part = np.asarray(im).copy()
                part[~m] = 0
                sub = Image.fromarray(part, "RGBA")
                keep += _feet_masks(sub, top, sub.getbbox() or box)
            else:
                keep.append(m)
        lm = sorted(keep, key=lambda m: np.nonzero(m)[1].mean())
    masks = (lm if face and len(lm) >= 2 else fm if len(fm) > len(lm) else lm) or _halves(im, top, box)
    if not face:  # de profil, une queue ou un ventre couché au sol (bien plus long que haut), ou qui ne touche pas le sol, n'est pas une patte
        legs_only = [m for m in masks if _is_leg(m) and np.nonzero(m)[0].max() >= y1 - 1 - max(2, round(zh * .15))]
        masks = legs_only if len(legs_only) >= 2 else masks
    arr = np.asarray(im)
    masks = _attach(masks, arr, top, reach=max(6, round(arr.shape[1] * .012)))  # griffes et bouts de doigts, à l'échelle de l'image
    if face:  # tout près du sol, rien n'est le ventre : les doigts écartés suivent leur pied (sinon ils restaient au sol)
        masks = _attach(masks, arr, y1 - max(2, round(zh * .12)), reach=arr.shape[1])
    body = arr.copy()
    body[top:] = 0
    rest = np.asarray(im.getchannel("A")) >= 128
    rest[:top] = False
    for m in masks:
        rest &= ~m
    out = np.zeros_like(arr)
    keep = np.zeros_like(arr)
    keep[rest] = arr[rest]  # le ventre (rattaché à aucune patte) reste en place, derrière les pattes
    if not face:  # le haut des cuisses reste aussi derrière : une patte qui pivote ne laisse pas de trou sous la hanche
        hip = np.zeros(arr.shape[:2], bool)
        hip[top:top + max(2, round(zh * .25))] = True
        keep[hip] = arr[hip]
    # de profil, 4 pattes (avant, avant, arrière, arrière) : chaque paire en opposition, en diagonale (avant proche avec arrière loin)
    signs = [1, -1, -1, 1] if len(masks) == 4 and not face else [1 if k % 2 == 0 else -1 for k in range(len(masks))]
    for k, m in enumerate(masks):
        sign = signs[k]
        leg = np.where(m[..., None], arr, 0).astype(arr.dtype)
        ahead = sign * phase > 0  # la patte qui avance (en l'air)
        ys, xs = np.nonzero(m)
        if face:
            # (5) de face ou de dos, la patte pivote vers l'avant ou l'arrière, dans la profondeur : au demi-pas celle qui
            # avance se lève (le pied monte, le haut passe derrière le corps) ; au pas complet elle est posée, et celle qui
            # s'éloigne de nous paraît plus courte (de face : la patte restée en arrière ; de dos : celle qui avance)
            # (la patte se replie depuis la hanche au lieu de monter d'un bloc : plus de trou dans le ventre au-dessus)
            hip, ln = int(ys.min()), int(ys.max()) - int(ys.min()) + 1
            lift = max(2, round(h * .07)) if ahead and abs(phase) == .5 else 0
            moved = _squash(leg, hip, max(.5, 1 - lift / ln)) if lift else leg
            if abs(phase) == 1 and ahead == (direction == "haut"):
                moved = _squash(moved, hip, .82)
        else:  # elle pivote d'un bloc depuis la hanche (plus de patte penchée en parallélogramme), le pied se lève au demi-pas
            hip = (xs.mean(), ys.min())
            moved = _rotate(leg, hip, sign * phase * g["angle"])
            lift = round(zh * .2) if ahead and abs(phase) == .5 else 0
            if lift:
                moved = np.concatenate([moved[lift:], np.zeros_like(moved[:lift])])
        out = np.where(moved[..., 3:] > 0, moved, out)
    body, keep = _upper(body, keep, arr, masks, top, box, direction, phase, g)
    dy = g["bob"] if abs(phase) >= 1 and h >= 24 else 0  # appui : le corps descend
    if dy:
        body = np.roll(body, dy, axis=0)
        body[:dy] = 0
    out = np.where(out[..., 3:] > 0, out, keep)
    out = np.where(body[..., 3:] > 0, body, out)  # le corps passe devant le haut des pattes
    return Image.fromarray(out.astype(np.uint8), "RGBA")


# ------------------------------------------------------------------ démarche : chaque espèce marche à sa façon
# angle : écart d'une patte au pas complet (degrés) ; bob : descente du corps à l'appui (px) ; nod : la tête plonge de plus
# à l'appui ; ms : durée d'une image dans l'Arène (avant la vitesse) ; sway : de face, le corps passe d'une patte à l'autre
GAIT = {
    "": dict(angle=26, bob=1, nod=1, ms=90, sway=1),
    "felin": dict(angle=28, bob=1, nod=0, ms=85, sway=0),  # souple : la tête reste droite, grands pas
    "insecte": dict(angle=16, bob=0, nod=0, ms=65, sway=0),  # petits pas rapides, le corps ne monte ni ne descend
    "oiseau": dict(angle=16, bob=1, nod=2, ms=75, sway=1),  # petits pas, la tête qui pique en avant
    "reptile": dict(angle=22, bob=0, nod=0, ms=100, sway=2),  # le corps se dandine d'un côté à l'autre
    "lourd": dict(angle=18, bob=2, nod=1, ms=120, sway=1),  # grand ou très lourd : lent, chaque pas l'enfonce
}
GAIT_WORDS = (
    ("insecte", r"insecte|araignee|fourmi|scarabee|mante|criquet|sauterelle|scorpion|cafard|coccinelle|acarien|crabe|"
                r"insect|spider|beetle|mantis|cricket|grasshopper|crab"),
    ("oiseau", r"oiseau|aigle|corbeau|hibou|chouette|faucon|poule|\bcoq\b|canard|pingouin|manchot|autruche|perroquet|"
               r"moineau|pigeon|\bbird|eagle|owl|crow|raven|chicken|duck|penguin"),
    ("felin", r"felin|\bchat|lynx|tigre|\blion|panthere|guepard|jaguar|\bloup|renard|chien|puma|leopard|"
              r"\bcat\b|feline|tiger|wolf|fox|\bdog\b|cheetah"),
    ("reptile", r"lezard|reptile|crocodile|alligator|salamandre|gecko|iguane|varan|cameleon|tortue|triton|"
                r"lizard|turtle|tortoise|newt"),
)
TAIL_ANGLE = 12  # degrés : la queue balance d'autant à chaque pas complet


def _measure(text, units):
    """« 1,5 m » -> 1.5 (units = {"m": 1, "cm": .01}) ; None si rien de lisible."""
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(" + "|".join(sorted(units, key=len, reverse=True)) + r")\b", fold_text(text))
    return float(m.group(1).replace(",", ".")) * units[m.group(2)] if m else None


def fold_text(text):
    return nb_core.fold(text or "")


def gait(description, brief=None, lore=None):
    """La démarche de la créature (une copie de GAIT, plus kind, wings, heads), d'après ses mots, sa taille et son poids."""
    b, lo = brief or {}, lore or {}
    text = fold_text(" ".join([description or "", b.get("en", ""), b.get("silhouette", ""), lo.get("espece", ""),
                               lo.get("description", "")]))
    kind = next((k for k, rx in GAIT_WORDS if re.search(rx, text)), "")
    if not kind and b.get("posture") == "plusieurs_pattes":
        kind = "insecte"
    kg = _measure(lo.get("poids"), {"kg": 1, "kilos": 1, "t": 1000, "tonnes": 1000, "tonne": 1000, "g": .001})
    m = _measure(lo.get("taille"), {"m": 1, "cm": .01, "metres": 1, "metre": 1})
    if (kg or 0) >= 300 or (m or 0) >= 3:
        kind = "lourd"
    heads = counts(description).get("head", 1) if description else 1
    return dict(GAIT[kind], kind=kind or "pattes", wings=has_wings(description or "", brief), heads=heads)


def walk_ms(g, vitesse=None):
    """La durée d'une image de marche dans l'Arène : la démarche, puis la vitesse (une créature rapide trotte)."""
    ms = g["ms"]
    if vitesse:
        ms *= max(.8, min(1.2, 1.2 - vitesse / 200))
    return round(ms)


def _shift(a, dx, dy):
    """Le tableau décalé de (dx, dy), sans retour de l'autre côté."""
    out = np.zeros_like(a)
    H, W = a.shape[:2]
    if abs(dx) >= W or abs(dy) >= H:
        return out
    out[max(0, dy):H + min(0, dy), max(0, dx):W + min(0, dx)] = a[max(0, -dy):H + min(0, -dy), max(0, -dx):W + min(0, -dx)]
    return out


def _move(a, region, dx, dy):
    """Les pixels de region déplacés de (dx, dy), par-dessus le reste ; leur ancienne place se vide."""
    if not (dx or dy) or not region.any():
        return a
    part = np.where(region[..., None], a, 0).astype(a.dtype)
    out = a.copy()
    out[region] = 0
    moved = _shift(part, dx, dy)
    return np.where(moved[..., 3:] > 0, moved, out)


def _upper(body, keep, arr, masks, top, box, direction, phase, g):
    """Le haut du corps suit le pas. De profil (tête à gauche) : la queue balance (1), la tête plonge à l'appui (2),
    les oreilles, crêtes et antennes traînent d'un pixel au demi-pas (36), plusieurs têtes bougent chacune à son rythme
    (38). De face / dos : le corps passe d'une patte sur l'autre (2). Avec des ailes, elles battent en marchant (3)."""
    x0, y0, x1, y1 = box
    h, w = y1 - y0, x1 - x0
    full, half = abs(phase) >= 1, abs(phase) == .5
    if g.get("wings"):  # les ailes battent aussi en marchant, moins qu'en vol
        body = np.asarray(flap(Image.fromarray(body.astype(np.uint8), "RGBA"), direction, phase * .6)).copy()
    if direction in ("bas", "haut"):
        s = g.get("sway", 0)
        if s and full:
            body = _shift(body, round(phase) * s, 0)
        return body, keep
    a = arr[..., 3] >= 128
    legs = np.zeros_like(a)
    for m in masks:
        legs |= m
    lx = np.nonzero(legs)[1]
    if not len(lx):
        return body, keep
    front, rear = int(lx.min()), int(lx.max())
    cols = np.arange(a.shape[1])[None, :]
    rows = np.arange(a.shape[0])[:, None]
    # (1) la queue : ce qui dépasse derrière les pattes arrière, tourné autour de sa racine
    tail = a & ~legs & (cols > rear + 1)
    if a.sum() * .02 <= tail.sum() <= a.sum() * .3 and tail[:, :rear + 6].any():
        root_ys = np.nonzero(tail[:, rear + 2:rear + 5].any(1))[0]
        root = (rear + 2, root_ys.mean() if len(root_ys) else np.nonzero(tail)[0].mean())
        part = np.where(tail[..., None], arr, 0).astype(arr.dtype)
        rot = _rotate(part, root, phase * TAIL_ANGLE)
        base = tail & (cols <= rear + 3)  # la racine ne bouge pas : pas de trou entre la croupe et la queue
        body, keep = body.copy(), keep.copy()
        body[tail], keep[tail] = 0, 0
        body = np.where(rot[..., 3:] > 0, rot, body)
        body[base] = arr[base]
    body_a = body[..., 3] > 0
    # (38) plusieurs têtes : chaque morceau du haut bouge à son rythme
    if g.get("heads", 1) >= 2:
        crown = body_a & (rows < y0 + h * .4)
        lab, n = ndimage.label(crown, structure=np.ones((3, 3)))
        sizes = ndimage.sum(crown, lab, range(1, n + 1))
        heads = [k + 1 for k, v in enumerate(sizes) if v >= crown.sum() * .1]
        if len(heads) >= 2:
            for i, k in enumerate(heads):
                sgn = 1 if i % 2 else -1
                body = _move(body, lab == k, sgn * round(phase) if full else 0, sgn if half and phase > 0 else -sgn if half else 0)
            return body, keep
    # (2) la tête plonge à l'appui : devant les pattes avant (à quatre pattes) ou le haut du corps (debout)
    nod = g.get("nod", 0) if full else 0
    if nod:
        head = body_a & (cols < front - 1) if w > h * .9 else body_a & (rows < y0 + h * .28)
        if head.sum() >= body_a.sum() * .04:
            body = _move(body, head, -1 if w > h * .9 and nod > 1 else 0, nod)
    # (36) oreilles, crête, antennes : elles traînent d'un pixel au demi-pas, quand le corps repart
    if half:
        crest = (body[..., 3] > 0) & (rows < y0 + round(h * .14) + nod)
        body = _move(body, crest, 1, 0)
    return body, keep


def _is_leg(m):
    ys, xs = np.nonzero(m)
    return len(xs) > 0 and np.ptp(xs) + 1 <= 1.6 * (np.ptp(ys) + 1) + 2


def _rotate(leg, pivot, deg):
    """La patte (RGBA, fond transparent) tournée d'un bloc autour de pivot, au plus proche voisin (pixels nets).
    deg > 0 : le pied part vers la gauche (l'avant, de profil tête à gauche)."""
    if not deg:
        return leg.copy()
    a = np.radians(deg)
    H, W = leg.shape[:2]
    ys, xs = np.nonzero(leg[..., 3])
    out = np.zeros_like(leg)
    if not len(xs):
        return out
    # seulement autour du pivot : une rotation garde les distances, rien n'arrive plus loin que le pixel le plus éloigné
    r = int(np.ceil(np.hypot(xs + .5 - pivot[0], ys + .5 - pivot[1]).max())) + 2
    X0, Y0 = max(0, int(pivot[0]) - r), max(0, int(pivot[1]) - r)
    yy, xx = np.mgrid[Y0:min(H, int(pivot[1]) + r + 1), X0:min(W, int(pivot[0]) + r + 1)]
    dx, dy = xx + .5 - pivot[0], yy + .5 - pivot[1]
    # inverse : pour chaque pixel d'arrivée, le pixel de départ
    sx = np.floor(dx * np.cos(a) + dy * np.sin(a) + pivot[0]).astype(int)
    sy = np.floor(-dx * np.sin(a) + dy * np.cos(a) + pivot[1]).astype(int)
    ok = (sx >= 0) & (sx < W) & (sy >= 0) & (sy < H)
    out[yy[ok], xx[ok]] = leg[sy[ok], sx[ok]]
    return out


def _attach(masks, arr, top, reach=6):
    """Les bouts de patte oubliés (un contour, une griffe) suivent la patte la plus proche à reach px au plus : ils ne
    restent plus sur place en double quand la patte bouge. Plus loin, c'est le ventre : il ne bouge pas."""
    if not masks:
        return masks
    a = arr[..., 3] >= 128
    a[:top] = False
    taken = np.zeros_like(a)
    for m in masks:
        taken |= m
    free = a & ~taken
    if not free.any():
        return masks
    owner = np.zeros(a.shape, int)
    for k, m in enumerate(masks):
        owner[m] = k + 1
    dist, (iy, ix) = ndimage.distance_transform_edt(~taken, return_indices=True)
    near = free & (dist <= reach)
    out = [m.copy() for m in masks]
    who = owner[iy, ix]
    for k in range(len(masks)):
        out[k] |= near & (who == k + 1)
    return out


def _squash(leg, top, s):
    """La patte raccourcie vers sa hanche (ligne top) : s < 1, vue en raccourci, le pied remonte."""
    out = np.zeros_like(leg)
    ys = np.arange(top, leg.shape[0])
    src = np.round(top + (ys - top) / s).astype(int)
    ok = src < leg.shape[0]
    out[ys[ok]] = leg[src[ok]]
    return out


def _walk_check():
    leg = np.zeros((20, 20, 4), np.uint8)
    leg[5:15, 10, :] = 255  # une patte verticale, hanche en (10.5, 5)
    r = _rotate(leg, (10.5, 5), 30)
    ys, xs = np.nonzero(r[..., 3])
    assert xs[ys.argmax()] < 10 and r[..., 3].astype(bool).sum() >= 8, "_rotate : le pied part vers l'avant, la patte reste entière"
    m = np.zeros((20, 20), bool)
    m[10:15, 5] = True
    arr = np.zeros((20, 20, 4), np.uint8)
    arr[10:15, 5:7, 3] = 255
    arr[10:12, 14:19, 3] = 255  # à 9 px : le ventre
    got = _attach([m], arr, 10)[0]
    assert got[10, 6] and not got[10, 16], "_attach : le contour suit la patte, le loin reste au corps"
    assert _measure("1,5 m", {"m": 1, "cm": .01}) == 1.5 and _measure("40 cm", {"m": 1, "cm": .01}) == .4
    assert gait("un grand loup")["kind"] == "felin" and gait("x", lore={"poids": "2 tonnes"})["kind"] == "lourd"
    z = np.zeros((5, 5, 4), np.uint8)
    z[1, 1] = 255
    assert _shift(z, 2, 1)[2, 3, 3] == 255 and _shift(z, -2, 0)[1, 4, 3] == 0
    tail = np.zeros((20, 30), bool)
    tail[15:18, 2:28] = True
    assert _is_leg(m) and not _is_leg(tail), "_is_leg : une queue couchée n'est pas une patte"
    # deux pattes séparées, qui se croisent 3 lignes près du sol : la zone des pattes remonte jusqu'au ventre
    im = Image.new("RGBA", (64, 64))
    im.paste((90, 90, 90, 255), (10, 10, 54, 40))  # le corps
    for x in (14, 40):
        im.paste((90, 90, 90, 255), (x, 40, x + 6, 64))
    im.paste((90, 90, 90, 255), (14, 54, 46, 57))
    assert leg_zone(im)[0] <= 41, ("leg_zone : les pattes entières", leg_zone(im)[0])
    sq = _squash(leg, 5, .8)
    assert np.nonzero(sq[..., 3])[0].max() < 14 and np.nonzero(sq[..., 3])[0].min() == 5, "_squash : le pied remonte, la hanche reste"


_walk_check()


def _rows(im, shift):
    """Chaque ligne y décalée de shift(y) px à l'horizontale."""
    out = Image.new("RGBA", im.size, (0, 0, 0, 0))
    for y in range(im.height):
        row = im.crop((0, y, im.width, y + 1))
        out.paste(row, (shift(y), y), row)
    return out


def _cols(im, shift):
    """Chaque colonne x décalée de shift(x) px à la verticale."""
    out = Image.new("RGBA", im.size, (0, 0, 0, 0))
    for x in range(im.width):
        col = im.crop((x, 0, x + 1, im.height))
        out.paste(col, (x, shift(x)), col)
    return out


# ------------------------------------------------------------------ ondulation (refaite en 1.10.0)
# Une vraie vague qui voyage le long du corps : 4 images distinctes, lues en boucle (TRAVEL_CYCLE), au lieu de 5 images
# lues en aller-retour où la vague ne faisait que monter et descendre sur place. phase (1, .5, -.5, -1) -> instant t.
TRAVEL_T = {1: 0., .5: .25, -.5: .5, -1: .75}
WALK_CYCLE = ["repos", "mi1", "pas1", "mi1", "repos", "mi2", "pas2", "mi2"]  # les pattes : aller-retour
TRAVEL_CYCLE = ["pas1", "mi1", "mi2", "pas2"]  # une vague : t = 0, .25, .5, .75, puis on recommence


def _travel(f):
    f.travel = True
    return f


def _t(phase):
    return TRAVEL_T.get(phase, phase % 1)


@_travel
def wave(im, direction, phase, amp=.05, waves=1.2):
    """Serpent, ver, chenille. Profil (tête à gauche) : des bosses partent de la queue et remontent jusqu'à la tête, le
    ventre reste au sol. Face / dos : le corps dessine un S qui file de la tête vers la queue, la tête reste en place."""
    im = im.convert("RGBA")
    box = im.getbbox()
    if not box or not phase:
        return im.copy()
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    t = _t(phase)
    if direction in ("bas", "haut"):
        a = max(2, round(w * .12))

        def shift(y):
            v = (y1 - y) / max(1, h) if direction == "bas" else (y - y0) / max(1, h)  # 0 à la tête, 1 au bout de la queue
            return round(a * v * math.sin(2 * math.pi * (waves * v - t)))
        return _rows(im, shift)
    bump = 1 if h < 16 else 2 if h < 40 else 3  # la hauteur d'une bosse, d'après l'épaisseur du corps
    arr = np.asarray(im)
    out = arr.copy()
    for x in range(x0, x1):
        u = (x - x0) / max(1, w)
        k = round(bump * max(0., math.sin(2 * math.pi * (waves * 1.5 * u + t))))
        ys = np.nonzero(arr[:, x, 3] >= 128)[0]
        if not k or not len(ys) or ys[0] - k < 0:
            continue
        mid = (ys[0] + ys[-1]) // 2  # le dos monte, le bas de la colonne (le ventre) ne bouge pas
        out[ys[0] - k:mid - k + 1, x] = arr[ys[0]:mid + 1, x]
        out[mid - k + 1:mid + 1, x] = arr[mid, x]
    return Image.fromarray(out, "RGBA")


@_travel
def swim(im, direction, phase):
    """Poisson : la tête reste, la vague grandit vers la nageoire de queue et file de la tête vers la queue."""
    im = im.convert("RGBA")
    box = im.getbbox()
    if not box or not phase:
        return im.copy()
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    t = _t(phase)
    if direction in ("bas", "haut"):
        a = max(2, round(w * .1))

        def shift(y):
            v = (y1 - y) / max(1, h) if direction == "bas" else (y - y0) / max(1, h)
            return round(a * v ** 1.5 * math.sin(2 * math.pi * (.8 * v - t)))
        return _rows(im, shift)
    a = max(2, round(max(w, h) * .06))
    return _cols(im, lambda x: round(a * ((x - x0) / max(1, w)) ** 1.5 * math.sin(2 * math.pi * (.8 * (x - x0) / max(1, w) - t))))


def _rescale(im, sx, sy):
    """La créature étirée (sx, sy), posée au même endroit (bas et centre gardés)."""
    box = im.getbbox()
    if not box:
        return im.copy()
    part = im.crop(box)
    nw, nh = max(1, round(part.width * sx)), max(1, round(part.height * sy))
    part = part.resize((nw, nh), Image.Resampling.NEAREST)
    out = Image.new("RGBA", im.size, (0, 0, 0, 0))
    out.paste(part, ((box[0] + box[2]) // 2 - nw // 2, box[3] - nh), part)
    return out


def hop(im, direction, phase):
    """Bond : +1 accroupi (tassé, élargi), -1 en extension (étiré, aminci) ; ±0.5 à mi-chemin."""
    t = abs(phase)
    return _rescale(im, 1 + .06 * t, 1 - .1 * t) if phase > 0 else _rescale(im, 1 - .05 * t, 1 + .08 * t) if phase < 0 else im.copy()


def jelly(im, direction, phase):
    """Gelée : elle s'étale puis se resserre en glissant."""
    t = abs(phase)
    return _rescale(im, 1 + .08 * t, 1 - .08 * t) if phase > 0 else _rescale(im, 1 - .04 * t, 1 + .05 * t) if phase < 0 else im.copy()


def roll(im, direction, phase):
    """Boule : de profil elle tourne sur elle-même ; de face et de dos, sa surface défile dans sa silhouette."""
    im = im.convert("RGBA")
    box = im.getbbox()
    if not box or not phase:
        return im.copy()
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    if direction not in ("bas", "haut"):
        return im.rotate(25 * phase, Image.Resampling.NEAREST, center=(cx, cy))
    part = im.crop(box)
    k = max(1, round(part.height * .12 * abs(phase))) * (1 if phase > 0 else -1)
    rgb = ImageChops.offset(part, 0, k)  # la texture défile (en boucle), la silhouette reste
    rgb.putalpha(part.getchannel("A"))
    out = Image.new("RGBA", im.size, (0, 0, 0, 0))
    out.paste(rgb, box[:2], rgb)
    return out


def sway(im, direction, phase):
    """Plante ou créature enracinée : le haut se balance, la base reste plantée."""
    im = im.convert("RGBA")
    box = im.getbbox()
    if not box or not phase:
        return im.copy()
    x0, y0, x1, y1 = box
    base = y0 + (y1 - y0) * .65
    a = max(2, round((x1 - x0) * .06))
    return _rows(im, lambda y: round(phase * a * (base - y) / max(1, base - y0)) if y < base else 0)


MOTION = {"rampe": wave, "nage": swim, "sautille": hop, "roule": roll, "glisse": jelly, "enracine": sway}


def floats(description, brief=None):
    """Elle flotte sans ailes : aucune image de mouvement, le flottement (un lent va-et-vient vertical) est joué à l'affichage."""
    return (brief or {}).get("posture") == "flotte" and not has_wings(description, brief)


def still(im, direction, phase):
    return im.copy()


@_travel
def drift(im, direction, phase):
    """Elle flotte : le bas (tentacules, queue, voile) ondule, la vague descend jusqu'au bout ; (6) le haut se balance
    doucement de l'autre côté, comme suspendu, et le corps respire (il s'étire un peu puis se tasse).
    Le lent va-et-vient vertical reste joué à l'affichage (arene.json : marche.flotte)."""
    im = im.convert("RGBA")
    box = im.getbbox()
    if not box or not phase:
        return im.copy()
    x0, y0, x1, y1 = box
    mid = y0 + (y1 - y0) * .4
    a, lean = max(2, round((x1 - x0) * .07)), max(1, round((x1 - x0) * .025))
    t = _t(phase)

    def shift(y):
        s = (y - mid) / max(1, y1 - mid)
        top = round(-lean * math.sin(2 * math.pi * t) * (mid - y) / max(1, mid - y0)) if y < mid else 0
        return top + (round(a * s ** 1.3 * math.sin(2 * math.pi * (.7 * s - t))) if y > mid else 0)
    out = _rows(im, shift)
    breath = round((y1 - y0) * .025 * math.cos(2 * math.pi * t))  # t = 0 : étiré, t = .5 : tassé
    return _rescale(out, 1, (y1 - y0 + breath) / (y1 - y0)) if breath else out


def walk_cycle(description, brief=None):
    """L'ordre de lecture de la marche (arene.json : marche.ordre)."""
    return TRAVEL_CYCLE if getattr(motion(description, brief), "travel", False) else WALK_CYCLE


def tidy(frame, rest):
    """Les miettes nées du calcul (3 px au plus, posées là où la pose de repos est vide) sont effacées."""
    a = np.asarray(frame)
    on = a[..., 3] >= 128
    lab, n = ndimage.label(on, structure=np.ones((3, 3)))
    if n < 2:
        return frame
    sizes = ndimage.sum(on, lab, range(1, n + 1))
    was = np.asarray(rest)[..., 3] >= 128
    out = a.copy()
    for k in np.nonzero(sizes <= 3)[0] + 1:
        m = lab == k
        if not (m & was).any():
            out[m] = 0
    return Image.fromarray(out, "RGBA")


def _drift_check():
    im = Image.new("RGBA", (64, 64))
    im.paste((200, 50, 50, 255), (20, 10, 44, 60))
    f = drift(im, "bas", .5)
    assert f.getpixel((32, 11))[3] and f.getbbox() != im.getbbox(), "drift : le haut reste en place à 1 px près, le bas ondule"
    w = [wave(im, "gauche", p) for p in (1, .5, -.5, -1)]
    assert len({x.tobytes() for x in w}) == 4, "wave : 4 instants distincts (la vague voyage)"
    crumb = im.copy()
    crumb.paste((1, 2, 3, 255), (5, 5, 6, 7))  # 2 px détachés du corps
    assert tidy(crumb, im).getbbox() == im.getbbox() and tidy(crumb, crumb).getbbox() == crumb.getbbox(), "tidy : miettes"


_drift_check()


def motion(description, brief=None):
    """Le mouvement procédural de la créature, f(image, direction, phase), ou None (elle marche sur ses pattes)."""
    p = (brief or {}).get("posture")
    if floats(description, brief):
        return drift
    if has_wings(description, brief) and (p in FLOATING or not has_legs(description, brief)):
        return flap
    if p in MOTION and not crawls_on_legs(description, brief):
        return MOTION[p]
    if not has_legs(description, brief):
        # le décalage de hover() disparaît quand les sprites sont alignés en bas : il faut une forme qui change
        return lambda im, d, ph: bob(im, ph)
    return None


assert fix_posture("une créature rampante à quatre pattes", {"posture": "rampe", "pattes": 0}) == {"posture": "quatre_pattes", "pattes": 4}
assert fix_posture("un ver rampant", {"posture": "rampe", "pattes": 0})["posture"] == "rampe"
assert motion("un insecte à six pattes qui rampe", {"posture": "rampe"}) is None and motion("un ver", {"posture": "rampe"}) is wave,     "un rampant qui a des pattes marche dessus"

WALK_PHASES = (1, .5, 0, -.5, -1)  # pas A, demi-pas A, repos, demi-pas B, pas B


def generate_walk(client, view, direction, description, seed, progress=lambda m: None, cancel=lambda: False, brief=None,
                  lore=None):
    """5 images : pas A, demi-pas A, repos, demi-pas B, pas B (lecture repos, ½A, A, ½A, repos, ½B, B, ½B).
    droite : miroir de la marche gauche.

    Les pas sont calculés depuis la pose de repos (pas redessinés par le modèle) : les pattes restent nettes et la
    créature identique d'une image à l'autre. Les demi-pas rendent le mouvement fluide. Sans pattes (œil, blob,
    serpent) : rebond, ondulation… selon la posture."""
    move = motion(description, brief)
    if move is None:  # sur ses pattes, à la façon de son espèce
        g = gait(description, brief, lore)
        return [view.copy() if not p else _finish(step_frame(view, direction, p, g), view) for p in WALK_PHASES]
    return [view.copy() if not p else _finish(move(view, direction, p), view) for p in WALK_PHASES]


def _finish(frame, rest):
    """Une image calculée : sans miettes, avec le même contour que la pose de repos."""
    import nb_quality
    return nb_quality.close_outline(tidy(frame, rest), rest)


def generate_creature(client, description, seed, progress=lambda m: None, cancel=lambda: False):
    """Tout d'un coup : {"design", "directions": {d: [versions]}, "walk": {d: [5 images]}}."""
    design = generate_design(client, description, seed, progress, cancel)
    dirs = generate_directions(client, design, seed, progress, cancel, legs=has_legs(description))
    walk = {}
    for d in ("bas", "haut", "gauche"):
        walk[d] = generate_walk(client, dirs[d][0], d, description, seed, progress, cancel)
    walk["droite"] = [ImageOps.mirror(im) for im in walk["gauche"]]
    return dict(design=design, directions=dirs, walk=walk)
