# -*- coding: utf-8 -*-
"""NeoBestiary Studio - les trois autres créations : objets, attaques et musiques, par les modèles locaux.

Objet   : une icône pixel art 64 px (FLUX.2 klein, détourée par BiRefNet), en 3 versions au choix.
Attaque : deux images clés lumineuses sur fond noir (même modèle), animées en planche 10 x 3 de 128 px
          (lancer sur le lanceur, trajet, impact sur la cible), comme les attaques signature de NeoMe ;
          la mécanique vient du catalogue (élément x geste).
Musique : ACE-Step 1.5 turbo (ComfyUI natif), un MP3.
Rangement : <bibliothèque>/<objets|attaques|musiques>/<slug>/item.json + fichiers. Une création à la fois (Jobs).
"""
import io
import json
import math
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance

import nb_comfy as nbc
import nb_library
import nb_moves
import nb_sprite as sp

FOLDER = {"objet": "objets", "attaque": "attaques", "musique": "musiques"}
CATS = {"arme": "weapon", "armure": "armor piece", "bouclier": "shield", "potion": "potion bottle", "nourriture": "food item",
        "outil": "tool", "bijou": "jewel", "trésor": "treasure", "clé": "key", "livre": "book or scroll",
        "objet du quotidien": "everyday object", "ingrédient": "crafting ingredient"}
ELEM_EN = {"feu": "fire", "eau": "water", "plante": "plant and leaves", "vent": "wind", "roche": "rock and earth", "glace": "ice",
           "foudre": "lightning", "lumière": "holy light", "ombre": "shadow", "esprit": "spirit and psychic", "acier": "steel",
           "poison": "poison", "insecte": "insect and chitin", "fée": "fairy magic and sparkles", "son": "sound and music",
           "temps": "time, clockwork and hourglass", "encre": "ink, paper and calligraphy", "gravité": "gravity and orbiting stones"}
SELF = {"onde", "explosion", "soin", "renfort", "cri"}        # l'effet reste sur le lanceur
TRAVEL = {"projectile", "souffle", "rayon", "pluie", "rafale", "fouet"}  # il part vers la cible
C, N = 128, 10                                                  # planche : 10 images de 128 px par ligne


# ------------------------------------------------------------------ objets de combat
# ce qu'un objet fait dans l'Arène, d'après sa catégorie et sa rareté : la nourriture et les potions soignent, une arme monte
# l'attaque, une armure ou un bouclier la défense, un bijou la vitesse. Écrit dans item.json (« combat »), lu par l'Arène.
_RAR = ["commun", "peu commun", "rare", "épique", "légendaire"]
_SOIN = [20, 25, 30, 40, 50]
_STAT = {"arme": ("attaque", "l'attaque"), "armure": ("defense", "la défense"), "bouclier": ("defense", "la défense"),
         "bijou": ("vitesse", "la vitesse")}


def combat_of(categorie, rarete):
    r = _RAR.index(rarete) if rarete in _RAR else 0
    if categorie in ("nourriture", "ingrédient", "potion"):
        soin = _SOIN[r] if categorie == "nourriture" else _SOIN[r] // 2 if categorie == "ingrédient" else round(_SOIN[r] * 1.5)
        purge = categorie == "potion" and r >= 2
        return dict(soin=soin, purge=purge, texte=f"soigne {soin} % des PV" + (" et guérit les statuts" if purge else ""))
    if categorie in _STAT:
        stat, nom = _STAT[categorie]
        n = 2 if r >= 3 else 1
        return dict(stat=stat, niveaux=n, texte=f"{nom} +{n}")
    return None


assert combat_of("nourriture", "rare")["soin"] == 30 and combat_of("potion", "légendaire")["purge"] and combat_of("clé", "rare") is None
assert combat_of("arme", "épique") == dict(stat="attaque", niveaux=2, texte="l'attaque +2")


def folder(root, kind):
    if kind not in FOLDER:
        raise ValueError("Création inconnue : " + str(kind))
    d = Path(root) / FOLDER[kind]
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_slot(root, kind, name):
    base = nb_library.slugify(name) or kind
    d, slug, n = folder(root, kind), base, 2
    while (d / slug).exists():
        slug, n = f"{base}_{n}", n + 1
    (d / slug).mkdir()
    return slug, d / slug


def load(root, kind, slug):
    f = folder(root, kind) / slug / "item.json"
    if not f.is_file():
        raise ValueError("Création introuvable.")
    rec = json.loads(f.read_text(encoding="utf-8"))
    return _fresh(rec) if kind == "attaque" else rec


def save(root, kind, slug, rec):
    rec["updated"] = int(time.time())
    (folder(root, kind) / slug / "item.json").write_text(json.dumps(rec, indent=1, ensure_ascii=False), encoding="utf-8")
    return rec


def items(root, kind):
    out = []
    for d in folder(root, kind).iterdir():
        f = d / "item.json"
        if d.name.startswith("_") or not f.is_file():
            continue
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        out.append(dict(_fresh(rec) if kind == "attaque" else rec, slug=d.name, kind=kind))
    return out


# ------------------------------------------------------------------ objets
def make_object(eng, client, root, rec, seed, progress, cancel, versions=3):
    """Icône 64 px (et le dessin détouré d'origine) en 3 versions."""
    slug, d = new_slot(root, "objet", rec["nom"])
    what = CATS.get(rec.get("categorie"), "item")
    prompt = (f"Pixel art icon of a single video game {what}: {rec['description']}. The whole object is visible and centered, "
              "seen from the front with a slight three-quarter angle, crisp pixels, clean dark outline, vivid colors, flat plain "
              "light gray background, no text, no hand, no character, no cast shadow.")
    rec.update(versions=0, version=0, seed=seed, created=int(time.time()), combat=combat_of(rec.get("categorie"), rec.get("rarete")))
    for i in range(versions):
        if cancel():
            raise RuntimeError("annulé")
        progress(f"Dessin de l'objet ({i + 1}/{versions})")
        im = eng.render(client, eng.MODEL, prompt, seed + i * 101, cut=True, cancel=cancel)
        im.save(d / f"hd_{i}.png")
        icon = sp.run_pipeline(im, dict(width=64, height=64, colors=24, outline=True, align="Centré", margin=2, bg_remove=False))
        icon.save(d / f"icone_{i}.png")
        rec["versions"] = i + 1
        save(root, "objet", slug, rec)
    return f"objet/{slug}"


def choose(root, kind, slug, delta):
    rec = load(root, kind, slug)
    n = rec.get("versions", 1)
    rec["version"] = (rec.get("version", 0) + int(delta)) % max(1, n)
    return dict(save(root, kind, slug, rec), slug=slug, kind=kind)


# ------------------------------------------------------------------ attaques
def _key(im):
    """L'effet lumineux sur fond noir -> RGBA (la lumière devient l'opacité), recadré au carré."""
    a = np.asarray(im.convert("RGB")).astype(float) / 255
    al = np.clip((a.max(-1) - .06) / .94, 0, 1) ** .8
    rgb = np.where(al[..., None] > 0, np.clip(a / np.maximum(a.max(-1, keepdims=True), 1e-3), 0, 1), 0)
    out = Image.fromarray(np.dstack([rgb * 255, al * 255]).astype(np.uint8), "RGBA")
    bb = out.getchannel("A").point(lambda v: 255 if v > 12 else 0).getbbox()
    out = out.crop(bb) if bb else out
    s = max(out.size)
    sq = Image.new("RGBA", (s, s))
    sq.alpha_composite(out, ((s - out.width) // 2, (s - out.height) // 2))
    return sq


def _pix(im):
    """Rendu pixel art : 128 px, 48 couleurs, opacité en 5 paliers."""
    im = im.resize((C, C), Image.LANCZOS)
    a = np.round(np.asarray(im.getchannel("A")).astype(float) / 255 * 4) / 4
    rgb = im.convert("RGB").quantize(48, method=Image.Quantize.MEDIANCUT).convert("RGB")
    return Image.fromarray(np.dstack([np.asarray(rgb), (a * 255).astype(np.uint8)]), "RGBA")


def _put(src, scale=1., rot=0., alpha=1., bright=1., dx=0, dy=0, sx=1.):
    """L'image clé posée au centre d'une case (4 x plus grande, réduite ensuite), à l'échelle, tournée, plus ou moins visible."""
    B = C * 4
    im = src.resize((max(2, int(B * scale * sx)), max(2, int(B * scale))), Image.LANCZOS)
    if rot:
        im = im.rotate(rot, Image.BICUBIC, expand=True)
    if bright != 1:
        im = Image.merge("RGBA", (*ImageEnhance.Brightness(im.convert("RGB")).enhance(bright).split(), im.getchannel("A")))
    if alpha < 1:
        im.putalpha(im.getchannel("A").point(lambda v: int(v * max(0., alpha))))
    out = Image.new("RGBA", (B, B))
    out.alpha_composite(im, ((B - im.width) // 2 + dx * 4, (B - im.height) // 2 + dy * 4))
    return out


def _stack(*layers):
    out = Image.new("RGBA", layers[0].size)
    for la in layers:
        out.alpha_composite(la)
    return out


def _ease(k):
    return 1 - (1 - k) ** 3


def _wipe(im, k, angle=1.):
    """L'image révélée en diagonale (un coup de griffe qui traverse) : k de 0 (rien) à 1 (toute)."""
    w, h = im.size
    yy, xx = np.mgrid[0:h, 0:w]
    m = ((xx / w + angle * (1 - yy / h)) / (1 + angle) <= k * 1.15).astype(np.uint8) * 255
    out = im.copy()
    out.putalpha(Image.fromarray(np.minimum(np.asarray(im.getchannel("A")), m)))
    return out


def _half(im, top):
    """La moitié haute ou basse de l'image (les deux mâchoires d'une morsure)."""
    w, h = im.size
    out = Image.new("RGBA", (w, h))
    box = (0, 0, w, h // 2) if top else (0, h // 2, w, h)
    out.alpha_composite(im.crop(box), box[:2])
    return out


def sheet_rows(genre, cast, hit):
    """Les 3 lignes de 10 images selon le geste : sur soi, vers la cible (trajet ou rayon), ou au contact.
    Chaque geste a son propre mouvement : sans lui, toutes les attaques créées se ressemblaient (une image qui grossit et tourne)."""
    blank = [Image.new("RGBA", (C * 4, C * 4)) for _ in range(N)]
    t = [i / (N - 1) for i in range(N)]
    burst = [_put(hit, .25 + .45 * _ease(k), rot=i * 6, alpha=1 - max(0, (k - .55) / .45) ** 1.5, bright=1.25) for i, k in enumerate(t)]
    if genre == "onde":  # deux anneaux qui s'élargissent l'un après l'autre
        return [_stack(_put(cast, .2 + .9 * k, alpha=1 - k), _put(hit, .1 + .9 * max(0, k - .35) / .65, alpha=(1 - k) * (k > .35))) for k in t], blank, blank
    if genre == "explosion":  # un éclair blanc, l'éclatement très vite, puis la fumée qui retombe
        return [_put(cast if k < .5 else hit, .15 + 1. * _ease(min(1, k * 2.2)), alpha=1 - max(0, k - .55) / .45, bright=2.2 - 1.2 * min(1, k * 3)) for k in t], blank, blank
    if genre == "cri":  # trois cercles d'onde concentriques, décalés
        return [_stack(*[_put(cast, .2 + .8 * ((k + j / 3) % 1), alpha=1 - (k + j / 3) % 1) for j in range(3)]) for k in t], blank, blank
    if genre in ("soin", "renfort"):  # l'aura monte autour du lanceur et s'efface en haut
        return [_stack(_put(cast, .55 + .1 * math.sin(k * math.pi), dy=int(10 - 28 * k), alpha=min(1, k * 3) * (1 - max(0, k - .7) / .3)),
                       _put(hit, .4 + .5 * k, alpha=(1 - k) * .6)) for k in t], blank, blank
    if genre == "pluie":  # des gouttes ou éclats qui tombent en nombre, puis de petites éclaboussures au sol
        row0 = [_put(cast, .25, dy=-14, alpha=min(1, k * 2), bright=1.2) for k in t]
        row1 = [_stack(*[_put(cast, .22, dx=(j * 23) % 40 - 20, dy=((i * 9 + j * 17) % 40) - 22) for j in range(5)]) for i in range(N)]
        row2 = [_stack(*[_put(hit, .12 + .2 * _ease(k), dx=d, dy=8, alpha=1 - k) for d in (-16, 0, 16)]) for k in t]
        return row0, row1, row2
    if genre == "rafale":  # une volée de traits : trois copies décalées qui filent
        row0 = [_put(cast, .2 + .1 * math.sin(k * math.pi), bright=1.3) for k in t]
        row1 = [_stack(*[_put(cast, .3, dx=((i * 11 + j * 13) % 26) - 13, dy=(j - 1) * 12, sx=1.5) for j in range(3)]) for i in range(N)]
        row2 = [_stack(*[_put(hit, .15 + .25 * _ease(max(0, k - j * .15)), dx=(j - 1) * 12, dy=(1 - j) * 8, alpha=1 - k) for j in range(3)]) for k in t]
        return row0, row1, row2
    if genre == "fouet":  # un arc qui balaie, pas une toupie
        row0 = [_put(cast, .3, rot=-40 + 20 * k, bright=1.2) for k in t]
        row1 = [_put(cast, .55, rot=-70 + 140 * _ease(k), sx=1.3) for k in t]
        return row0, row1, burst
    if genre == "griffe":  # les marques de griffes qui traversent en diagonale, puis l'éclat
        return blank, blank, [_stack(_put(_wipe(cast, min(1, k * 1.8)), .8, alpha=1 - max(0, k - .7) / .3),
                                     _put(hit, .2 + .5 * _ease(max(0, k - .35) / .65), alpha=(1 - k) * (k > .35))) for k in t]
    if genre == "morsure":  # les deux mâchoires qui se referment, puis la marque des crocs
        return blank, blank, [_stack(_put(_half(cast, True), .8, dy=int(-16 * (1 - min(1, k * 2)))),
                                     _put(_half(cast, False), .8, dy=int(16 * (1 - min(1, k * 2)))),
                                     _put(hit, .3 + .4 * _ease(max(0, k - .45) / .55), alpha=(1 - k) * (k > .45))) if k < .75 else
                              _put(hit, .7, alpha=(1 - k) * 3) for k in t]
    if genre == "charge":  # la traînée de vitesse qui arrive de la gauche, puis le choc
        return blank, blank, [_stack(_put(cast, .6, dx=int(-30 * (1 - min(1, k * 2))), sx=1.6 - .6 * min(1, k * 2), alpha=1 - max(0, k - .5) / .5),
                                     _put(hit, .3 + .6 * _ease(max(0, k - .4) / .6), alpha=(1 - k) * (k > .4))) for k in t]
    if genre in ("corne", "dard"):  # une estocade rapide vers l'avant, puis une petite percée
        return blank, blank, [_stack(_put(cast, .5, dx=int(-24 + 30 * _ease(min(1, k * 2.5))), alpha=1 - max(0, k - .5) / .5),
                                     _put(hit, .15 + .35 * _ease(max(0, k - .3) / .7), alpha=(1 - k) * (k > .3))) for k in t]
    if genre == "etreinte":  # les anneaux qui tournent en se resserrant sur la cible
        return blank, blank, [_stack(_put(cast, .9 - .45 * _ease(k), rot=i * 25, alpha=1 - max(0, k - .75) / .25),
                                     _put(hit, .3 + .3 * k, alpha=k * (1 - k) * 2)) for i, k in enumerate(t)]
    if genre in SELF:
        row0 = [_stack(_put(cast, .3 + .6 * _ease(min(1, k * 1.6)), rot=i * 7, alpha=1 - max(0, k - .6) / .4, bright=1 + .3 * k),
                       _put(hit, .2 + .8 * _ease(k), alpha=(1 - k) * .8)) for i, k in enumerate(t)]
        return row0, blank, blank
    if genre in TRAVEL:
        row0 = [_put(cast, .18 + .2 * math.sin(k * math.pi), rot=i * 12, bright=1.3) for i, k in enumerate(t)]
        if genre in ("rayon", "souffle"):  # une bande qui défile et vacille
            arr = np.asarray(cast)
            row1 = [_put(Image.fromarray(np.roll(arr, i * 37 % cast.width, axis=1)), .7, sx=1.6, bright=1 + .25 * (i % 2 * 2 - 1))
                    for i in range(N)]
        else:
            row1 = [_put(cast, .5 + .05 * math.sin(i), rot=i * 36) for i in range(N)]
        row2 = [_put(hit, .25 + .45 * _ease(k), rot=i * 6, alpha=1 - max(0, (k - .55) / .45) ** 1.5, bright=1.25) for i, k in enumerate(t)]
        return row0, row1, row2
    row2 = [_stack(_put(cast, .5 + .4 * _ease(min(1, k * 2)), rot=-20 + 8 * i, alpha=1 - k),  # contact : la frappe puis l'éclat
                   _put(hit, .25 + .45 * _ease(k), alpha=1 - k ** 2, bright=1.2)) for i, k in enumerate(t)]
    return blank, blank, row2


def show(genre, color):
    """La mise en scène (même format que celle de NeoMe dans le Studio) : [début ms, ligne, 0 lanceur / 1 cible, taille, ms par image, options].
    Taille C : la planche à sa taille réelle, jamais étirée (sinon les pixels bavent)."""
    if genre in SELF:
        return dict(len=1600, hitAt=480, parts=[[0, 0, 0, C, 90, {}]], rings=[[480, 0, 50, color, 520, 3]])
    if genre in ("rayon", "souffle"):
        return dict(len=1700, hitAt=620, parts=[[0, 0, 0, C, 45, {"dx": 4, "dy": -10}], [420, 1, 0, C // 2, 40, {"dx": 12, "dy": -10, "beam": True, "until": 820}],
                                                 [520, 2, 1, C, 55, {"dy": -6}]], rings=[[620, 1, 55, color, 480, 3]])
    if genre in TRAVEL:
        return dict(len=1900, hitAt=780, parts=[[0, 0, 0, C, 40, {"dy": -8, "until": 420}], [360, 1, 0, C, 50, {"dx": 18, "dy": -8, "travel": 420, "until": 780}],
                                                 [780, 2, 1, C, 55, {"dy": -6}]], rings=[[780, 1, 60, color, 520, 3]])
    return dict(len=1500, hitAt=320, parts=[[200, 2, 1, C, 55, {"dy": -6}]], rings=[[320, 1, 55, color, 480, 3]])


def _fresh(rec):
    """Une attaque créée avant ces réglages : sa mise en scène recalculée (tailles nettes)."""
    import nb_attack
    if rec.get("genre") in nb_moves.GENRES and rec.get("element") in nb_attack.PALETTE:
        rec["show"] = show(rec["genre"], nb_attack.PALETTE[rec["element"]][2])
    return rec


# le geste de chaque attaque, en toutes lettres pour le modèle : sans lui, il dessinait une boule d'énergie quel que soit le geste
GESTE_EN = {
    "griffe": ("three sharp parallel claw slash marks tearing diagonally through the air", "a burst of three crossed claw scratches"),
    "morsure": ("a pair of huge fanged jaws snapping shut, upper and lower rows of teeth", "jagged tooth marks and a bite shockwave"),
    "charge": ("a rushing body-sized impact trail with speed lines, a battering charge", "a heavy collision shockwave with debris"),
    "fouet": ("a long whip-like tail or vine lashing in a curved arc", "a crack of a whip, a thin curved slash with sparks"),
    "corne": ("a single huge pointed horn thrusting forward like a spear", "a piercing star-shaped stab burst"),
    "dard": ("a thin sharp stinger needle jabbing forward, dripping venom", "a small puncture burst with venom droplets"),
    "etreinte": ("thick coiling tendrils wrapping in a tight ring, squeezing and constricting, a crushing hug", "a ring of tightened coils crushing the center"),
    "projectile": ("a single compact missile orb flying forward with a short trail", "the orb bursting on contact into shards"),
    "souffle": ("a wide cone of breath spreading out from a mouth, billowing", "a cloud of breath washing over the target"),
    "rayon": ("a straight thin continuous laser beam", "a bright beam impact flare"),
    "cri": ("concentric sound wave arcs and rings spreading outward from a screaming mouth, a roar shockwave", "vibrating sound rings and musical shock arcs"),
    "rafale": ("many fast thin streaks shooting forward in a volley", "a scatter of small streak impacts"),
    "pluie": ("many drops or shards falling from the sky in a downpour", "a splash zone of many small impacts on the ground"),
    "onde": ("expanding rings radiating all around from the center", "a wide circular ripple"),
    "explosion": ("a round blast exploding outward in every direction", "a huge round explosion with smoke"),
    "soin": ("soft rising sparkles and glowing crosses, a healing aura, no damage", "a gentle glow of healing light"),
    "renfort": ("a protective aura with upward arrows, a power-up", "a shining shield aura"),
}
assert set(GESTE_EN) == set(nb_moves.GENRES), set(nb_moves.GENRES) ^ set(GESTE_EN)


def make_attack(eng, client, root, rec, seed, progress, cancel, verify=None):
    """verify(images, texte) -> [{"score", "missing"}] : le modèle de vision note chaque dessin ; un dessin sous 5/10 est refait
    une fois, et le meilleur des deux est gardé."""
    import nb_attack
    e, g = rec["element"], rec["genre"]
    if (e, g) not in nb_moves.CATALOG:
        raise ValueError("Élément ou geste inconnu.")
    rec["rarete"] = nb_moves.get(e, g)["rarete"]  # la rareté vient de sa mécanique (catalogue)
    slug, d = new_slot(root, "attaque", rec["nom"])
    base = ("Pixel art video game attack effect sprite, crisp pixels, centered, isolated on a pure black background, "
            "no character, no creature, no text. ")
    cast, hit = GESTE_EN[g]
    progress("Lecture de la description")
    try:  # la plume coupe la description en deux consignes précises (l'énergie, l'impact) ; sans elle, la description telle quelle
        import nb_writer
        b = nb_writer.attack_brief(rec["description"], e, g, seed)
        what_cast, what_hit = b["cast"], b["impact"]
    except Exception:
        what_cast = what_hit = rec["description"]
    keys = (base + f"Draw exactly this: {what_cast}. Every shape, color and detail of this description matters most. "
                   f"Its general shape: {cast}. Element: {ELEM_EN[e]}.",
            base + f"Draw exactly this impact: {what_hit}. Every shape, color and detail of this description matters most. "
                   f"Its general shape: {hit}. Element: {ELEM_EN[e]}.")
    raw = []
    for i, prompt in enumerate(keys):
        if cancel():
            raise RuntimeError("annulé")
        progress("Dessin de l'effet" if i == 0 else "Dessin de l'impact")
        raw.append(eng.render(client, eng.MODEL, prompt, seed + i * 77, cancel=cancel))
    texts = (what_cast, what_hit)
    if verify:  # le contrôle visuel : chaque dessin comparé à sa consigne
        try:
            progress("Vérification des dessins")
            sc = [verify([im], t)[0]["score"] for im, t in zip(raw, texts)]
            for i, s in enumerate(sc):
                if s >= 5 or cancel():
                    continue
                progress(("Nouveau dessin de l'effet" if i == 0 else "Nouveau dessin de l'impact") + f" ({s}/10)")
                again = eng.render(client, eng.MODEL, keys[i], seed + i * 77 + 9001, cancel=cancel)
                s2 = verify([again], texts[i])[0]["score"]
                if s2 > s:
                    raw[i], sc[i] = again, s2
            rec["controle"] = sc
        except Exception as exc:  # pas de modèle de vision, ou il ne répond pas : les dessins restent tels quels
            print("attaque non vérifiée :", exc)
    imgs = []
    for i, im in enumerate(raw):
        im.save(d / f"cle_{i}.png")
        imgs.append(_key(im))
    progress("Animation de la planche")
    sheet = _sheet(g, *imgs)
    sheet.save(d / "planche.png", optimize=True)
    thumb_from(sheet, g).save(d / "vignette.png")
    m = {k: v for k, v in nb_moves.CATALOG[(e, g)].items() if k not in ("id", "nom")}
    rec.update(m, mecanique=nb_moves.describe(m), show=show(g, nb_attack.PALETTE[e][2]), seed=seed, created=int(time.time()))
    save(root, "attaque", slug, rec)
    return f"attaque/{slug}"


def thumb_from(sheet, genre):
    """La case la plus fournie de la ligne qui compte (impact, ou le lanceur pour les effets sur soi)."""
    row = 0 if genre in SELF else 2
    cells = [sheet.crop((i * C, row * C, (i + 1) * C, (row + 1) * C)) for i in range(N)]
    return max(cells, key=lambda c: np.asarray(c.getchannel("A")).sum())


def catalog_thumbs(root):
    """Vignettes 64 px des attaques du catalogue (l'image d'impact de leur effet), faites une fois."""
    import nb_attack
    d = folder(root, "attaque") / "_catalogue"
    d.mkdir(exist_ok=True)
    for (e, g) in nb_moves.CATALOG:
        f = d / f"{e}_{g}.png"
        if f.is_file():
            continue
        frames = [nb_attack._effect(g, e, i) for i in (2, 1, 0)]
        im = next((x for x in frames if x is not None and x.getbbox()), Image.new("RGBA", (64, 64)))
        im.save(f)
    return d


def _square(im):
    bb = im.getbbox()
    im = im.crop(bb) if bb else im
    s = max(im.size)
    sq = Image.new("RGBA", (s, s))
    sq.alpha_composite(im, ((s - im.width) // 2, (s - im.height) // 2))
    return sq


def _sheet(genre, cast, hit):
    sheet = Image.new("RGBA", (N * C, 3 * C))
    for r, row in enumerate(sheet_rows(genre, cast, hit)):
        for i, f in enumerate(row):
            sheet.alpha_composite(_pix(f), (i * C, r * C))
    return sheet


def catalog_sheet(root, e, g):
    """La planche animée (10 images x 3 temps) d'une attaque du catalogue, tirée de son effet en jeu, faite une fois."""
    import nb_attack
    if (e, g) not in nb_moves.CATALOG:
        raise ValueError("Attaque inconnue.")
    f = folder(root, "attaque") / "_catalogue" / f"{e}_{g}_anim5.png"
    if not f.is_file():
        f.parent.mkdir(parents=True, exist_ok=True)
        fr = [nb_attack._effect(g, e, i) for i in range(4)]
        if g in TRAVEL:  # les effets du jeu partent vers le bas : ici, de profil, vers la cible à droite
            fr = [nb_attack._orient(x, "droite") for x in fr]
        fr = [x if x is not None and x.getbbox() else None for x in fr]
        if not any(fr):
            fr = [Image.new("RGBA", (8, 8))] * 4
        fr = [x or next(y for y in fr if y) for x in fr]

        def cell(im, fade=1.):  # l'effet centré (sur ses pixels bien visibles) dans une case de C px, à la taille du jeu
            bb = im.getchannel("A").point(lambda v: 255 if v > 60 else 0).getbbox()
            sq = _square(im.crop(bb) if bb else im)
            if fade < 1:
                sq.putalpha(sq.getchannel("A").point(lambda v: int(v * fade)))
            out = Image.new("RGBA", (C, C))
            out.alpha_composite(sq, ((C - sq.width) // 2, (C - sq.height) // 2))
            return out
        play = [cell(fr[0]), cell(fr[0]), cell(fr[1]), cell(fr[1]), cell(fr[2]), cell(fr[2]), cell(fr[3]), cell(fr[3]),
                cell(fr[3], .6), cell(fr[3], .3)]
        hit = [cell(fr[2]), cell(fr[2]), cell(fr[2]), cell(fr[3]), cell(fr[3]), cell(fr[3]), cell(fr[3], .8), cell(fr[3], .6),
               cell(fr[3], .4), cell(fr[3], .2)]
        blank = [Image.new("RGBA", (C, C))] * N
        rows = (play, blank, blank) if g in SELF else \
               (play[:4] * 2 + play[2:4], [cell(fr[1]) for i in range(N)], hit) if g in TRAVEL else (blank, blank, hit)
        sheet = Image.new("RGBA", (N * C, 3 * C))
        for r, row in enumerate(rows):
            for i, im in enumerate(row):
                sheet.alpha_composite(im, (i * C, r * C))
        sheet.save(f, optimize=True)
    return f


def catalog():
    import nb_attack
    return [dict(m, slug=f"_catalogue/{m['element']}_{m['genre']}", kind="attaque", source="catalogue",
                 mecanique=nb_moves.describe(m), show=show(m["genre"], nb_attack.PALETTE[m["element"]][2]))
            for m in nb_moves.CATALOG.values()]


# ------------------------------------------------------------------ musiques
MUSIC_FILES = ("acestep_v1.5_turbo.safetensors", "qwen_0.6b_ace15.safetensors", "qwen_1.7b_ace15.safetensors", "ace_1.5_vae.safetensors")
TEMPO = {"lent": 80, "moyen": 110, "rapide": 140}
# l'ambiance : sa tonalité et quelques mots pour le modèle de musique
AMBIANCES = {"joyeuse": ("C major", "bright, cheerful"), "sombre": ("A minor", "dark, ominous"),
             "paisible": ("F major", "peaceful, calm, soft dynamics"), "épique": ("D minor", "epic, grand, powerful, cinematic build-up"),
             "mystérieuse": ("E minor", "mysterious, enigmatic, suspenseful, sparse"), "mélancolique": ("B minor", "melancholic, nostalgic, bittersweet"),
             "tendue": ("G minor", "tense, urgent, anxious, pulsing"), "festive": ("G major", "festive, celebratory, upbeat, dancing"),
             "héroïque": ("D major", "heroic, triumphant, adventurous, soaring melody"), "rêveuse": ("A major", "dreamy, ethereal, floating, airy"),
             "étrange": ("F# minor", "weird, quirky, off-kilter, playful dissonance"), "romantique": ("E major", "romantic, tender, warm, heartfelt")}


def music_graph(tags, lyrics, seconds, seed, bpm, keyscale, language):
    return {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": MUSIC_FILES[0], "weight_dtype": "default"}},
        "2": {"class_type": "DualCLIPLoader", "inputs": {"clip_name1": MUSIC_FILES[1], "clip_name2": MUSIC_FILES[2], "type": "ace", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": MUSIC_FILES[3]}},
        "4": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3}},
        "5": {"class_type": "TextEncodeAceStepAudio1.5", "inputs": {
            "clip": ["2", 0], "tags": tags, "lyrics": lyrics, "seed": seed, "bpm": bpm, "duration": float(seconds), "timesignature": "4",
            "language": language, "keyscale": keyscale, "generate_audio_codes": True, "cfg_scale": 2.0, "temperature": .85,
            "top_p": .9, "top_k": 0, "min_p": 0.0}},
        "6": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["5", 0]}},
        "7": {"class_type": "EmptyAceStep1.5LatentAudio", "inputs": {"seconds": float(seconds), "batch_size": 1}},
        "8": {"class_type": "KSampler", "inputs": {"model": ["4", 0], "seed": seed, "steps": 8, "cfg": 1.0, "sampler_name": "euler",
                                                   "scheduler": "simple", "positive": ["5", 0], "negative": ["6", 0],
                                                   "latent_image": ["7", 0], "denoise": 1.0}},
        "9": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "SaveAudioMP3", "inputs": {"audio": ["9", 0], "filename_prefix": "neobestiary/musique", "quality": "V0"}},
    }


def make_music(client, root, rec, seed, progress, cancel):
    seconds = max(10, min(240, int(rec.get("duree") or 60)))
    lyrics = (rec.get("paroles") or "").strip()
    key, mood = AMBIANCES.get(rec.get("ambiance"), AMBIANCES["joyeuse"])
    g = music_graph(rec["description"] + ", " + mood, lyrics or "[Instrumental]", seconds, seed, TEMPO.get(rec.get("tempo"), 110),
                    key, "fr" if lyrics else "en")
    slug, d = new_slot(root, "musique", rec["nom"])
    progress(f"Composition ({seconds} s de musique)")
    entry = client.wait(client.queue(g), cancel=cancel)
    files = [a for out in entry.get("outputs", {}).values() for a in out.get("audio", [])]
    if not files:
        raise nbc.ComfyError("ComfyUI n'a renvoyé aucun son.")
    (d / "musique.mp3").write_bytes(client.fetch_image(files[0]))
    rec.update(duree=seconds, seed=seed, created=int(time.time()))
    save(root, "musique", slug, rec)
    return f"musique/{slug}"


if __name__ == "__main__":  # contrôle sans GPU : planches et mises en scène pour chaque famille de geste
    cast = Image.new("RGB", (256, 256))
    from PIL import ImageDraw
    ImageDraw.Draw(cast).ellipse((60, 60, 196, 196), fill=(255, 120, 40))
    k = _key(cast)
    assert k.mode == "RGBA" and k.size[0] == k.size[1]
    for g in nb_moves.GENRES:
        rows = sheet_rows(g, k, k)
        assert len(rows) == 3 and all(len(r) == N for r in rows)
        s = show(g, "#ffffff")
        assert s["hitAt"] < s["len"] and all(p[1] in (0, 1, 2) for p in s["parts"])
    assert all(k.split()[1] in ("major", "minor") for k, _ in AMBIANCES.values())
    assert set(TEMPO) and len(catalog()) == len(nb_moves.CATALOG) and sorted(music_graph("x", "y", 30, 1, 100, "C major", "en"), key=int) == [str(i) for i in range(1, 11)]
    print("ok")
