# -*- coding: utf-8 -*-
"""NeoBestiary Studio - contrôle des images finies d'une créature, sans carte graphique : une note de 1 à 5 et la liste
de ce qui cloche, pour savoir lesquelles reprendre en premier (page Contrôle, bibliothèque).

Mesuré sur les sprites 64x64 du dossier sprites/ : pattes trouvées, mouvement de la marche, miettes, contour, cohérence
des directions (taille, couleurs), attaques dont le nom annonce un autre geste, stats qui ne collent pas au corps.
"""
import numpy as np
from PIL import Image
from scipy import ndimage

import nb_stats

DIRS = ("bas", "gauche", "droite", "haut")
WALK = ("pas1", "mi1", "mi2", "pas2")
WEIGHT = {"grave": 1.5, "moyen": 1, "leger": .5}


def _load(sd, key):
    p = sd / f"{key}.png"
    return Image.open(p).convert("RGBA") if p.is_file() else None


def _alpha(im):
    return np.asarray(im)[..., 3] >= 128


def crumbs(im, rest=None):
    """Morceaux de 3 px au plus détachés du corps (et absents de la pose de repos)."""
    a = _alpha(im)
    lab, n = ndimage.label(a, structure=np.ones((3, 3)))
    if n < 2:
        return 0
    sizes = ndimage.sum(a, lab, range(1, n + 1))
    was = _alpha(rest) if rest is not None else np.zeros_like(a)
    return sum(1 for k in np.nonzero(sizes <= 3)[0] + 1 if not ((lab == k) & was).any())


def edge(a):
    """Les pixels du bord de la silhouette (un voisin transparent)."""
    pad = np.pad(a, 1)
    inner = pad[:-2, 1:-1] & pad[2:, 1:-1] & pad[1:-1, :-2] & pad[1:-1, 2:]
    return a & ~inner


def outline_color(rest):
    """La couleur du contour de la créature (la plus fréquente du bord si elle en fait au moins 45 %), ou None."""
    arr = np.asarray(rest)
    e = edge(_alpha(rest))
    if e.sum() < 8:
        return None
    cols, counts = np.unique(arr[e][:, :3], axis=0, return_counts=True)
    k = counts.argmax()
    return tuple(int(v) for v in cols[k]) if counts[k] >= e.sum() * .45 else None


def outline_breaks(frame, rest):
    """Part du bord de l'image calculée qui n'a pas la couleur du contour (le contour « cassé »), 0 si pas de contour."""
    oc = outline_color(rest)
    if oc is None:
        return 0.
    e = edge(_alpha(frame))
    if not e.any():
        return 0.
    arr = np.asarray(frame)[..., :3]
    return float((np.abs(arr[e].astype(int) - oc).sum(1) > 30).mean())


def close_outline(frame, rest):
    """(17) Le même contour partout : un bord nouvellement à l'air dans une image calculée (une patte qui a pivoté, un
    corps tassé) reprend la couleur du contour de la pose de repos. Rien si la créature n'a pas de contour net."""
    oc = outline_color(rest)
    if oc is None:
        return frame
    arr = np.asarray(frame).copy()
    e = edge(arr[..., 3] >= 128) & ~edge(_alpha(rest))
    off = np.abs(arr[..., :3].astype(int) - oc).sum(-1) > 30
    fix = e & off
    arr[fix, :3] = oc
    return Image.fromarray(arr, "RGBA")


def _hist(im):
    arr = np.asarray(im)
    px = arr[arr[..., 3] >= 128][:, :3] // 48
    keys, counts = np.unique(px, axis=0, return_counts=True)
    return {tuple(k): c / max(1, counts.sum()) for k, c in zip(keys, counts)}


def color_overlap(a, b):
    ha, hb = _hist(a), _hist(b)
    return sum(min(v, hb.get(k, 0)) for k, v in ha.items())


def body_class(lore):
    """« lourde », « legere » ou « » d'après la taille et le poids écrits dans la fiche (la même règle que nb_stats)."""
    return nb_stats.body_class((lore or {}).get("poids"), (lore or {}).get("taille"))


def check(sd, rec, legs=True, engine=None):
    """{"note": 1..5, "problemes": [{"code", "texte", "gravite"}], "mesures": {...}} pour le dossier de sprites sd.
    engine : le moteur (leg_zone, _leg_masks…) pour chercher les pattes ; None : on ne les cherche pas."""
    pb, me = [], {}
    add = lambda code, texte, g: pb.append(dict(code=code, texte=texte, gravite=g))
    rests = {d: _load(sd, f"{d}_repos") for d in DIRS}
    if rests["bas"] is None:
        return dict(note=1, problemes=[dict(code="vide", texte="Pas encore de sprites.", gravite="grave")], mesures={})
    # pattes trouvées (profil)
    side = rests.get("gauche")
    if legs and engine is not None and side is not None:
        top, box = engine.leg_zone(side)
        found = 0
        if top is not None:
            masks = engine._leg_masks(side, top) or engine._feet_masks(side, top, box)
            found = len(masks)
        me["pattes"] = found
        if top is None:
            add("pattes", "Pas de pattes trouvées de profil : elle glisse au lieu de marcher.", "grave")
        elif found < 2:  # elles se touchent (une devant l'autre) : le pas se fait en deux moitiés, à petits pas
            add("pattes", "Ses pattes se touchent de profil : elle marche à petits pas.", "leger")
    if rec.get("profil_a_refaire"):  # la fiche a été corrigée (fix_posture), le profil vient de l'ancienne posture
        add("posture", "Son profil a été dessiné avec une mauvaise posture : à redessiner.", "moyen")
    # mouvement, miettes, contour de la marche
    moves, crumb, breaks = [], 0, []
    for d in ("gauche", "bas"):
        rest = rests.get(d)
        if rest is None:
            continue
        ra = _alpha(rest)
        for f in WALK:
            fr = _load(sd, f"{d}_{f}")
            if fr is None:
                continue
            moves.append(float((ra != _alpha(fr)).sum() / max(1, ra.sum())))
            crumb += crumbs(fr, rest)
            breaks.append(outline_breaks(fr, rest) - outline_breaks(rest, rest))
    if moves:
        me["mouvement"] = round(max(moves), 3)
        if max(moves) < .03 and not (rec.get("brief") or {}).get("posture") == "flotte":
            add("immobile", "Elle bouge à peine en marchant.", "moyen")
    me["miettes"] = crumb
    if crumb >= 6:
        add("miettes", f"{crumb} miettes de pixels détachées dans la marche.", "leger" if crumb < 15 else "moyen")
    if breaks:
        me["contour"] = round(max(breaks), 3)
        if max(breaks) > .12:
            add("contour", "Le contour se casse dans certaines images de marche.", "leger")
    # cohérence des directions
    hs = {d: (im.getbbox() or (0, 0, 0, 0)) for d, im in rests.items() if im is not None}
    heights = {d: b[3] - b[1] for d, b in hs.items() if b[3] > b[1]}
    if len(heights) >= 2 and min(heights.values()) > 0:
        ratio = max(heights.values()) / min(heights.values())
        me["tailles"] = round(ratio, 2)
        posture = (rec.get("brief") or {}).get("posture", "")
        limit = 1.9 if posture in ("quatre_pattes", "plusieurs_pattes", "rampe", "nage") else 1.4  # couchée : plus basse de profil
        if ratio > limit:
            add("tailles", "Pas la même taille selon la direction.", "moyen")
    over = [color_overlap(rests["bas"], rests[d]) for d in ("gauche", "haut") if rests.get(d) is not None]
    if over:
        me["couleurs"] = round(min(over), 2)
        if min(over) < .45:
            add("couleurs", "Les couleurs changent d'une direction à l'autre.", "moyen")
    # attaques
    k = (rec.get("lore") or {}).get("combat") or {}
    for a in k.get("attaques", []):
        said = nb_stats.name_genre(a.get("nom"))
        if said and said != a.get("genre"):
            add("attaque", f"« {a.get('nom')} » annonce un autre geste que le sien ({a.get('genre')}).", "leger")
    # stats et corps
    st, cls = k.get("stats") or {}, body_class(rec.get("lore"))
    if st and cls:
        tank, quick = st.get("pv", 0) + st.get("defense", 0), st.get("vitesse", 0)
        if cls == "lourde" and quick > max(st.get("pv", 0), st.get("defense", 0)):
            add("stats", "Très grande ou très lourde, mais plus rapide que solide.", "leger")
        if cls == "legere" and tank > 2.6 * max(1, quick):
            add("stats", "Petite et légère, mais lente et très solide.", "leger")
    note = max(1, round(5 - sum(WEIGHT[p["gravite"]] for p in pb)))
    return dict(note=note, problemes=pb, mesures=me)


if __name__ == "__main__":  # auto-test : une créature propre (note 5) puis la même cassée
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    im = Image.new("RGBA", (64, 64))
    im.paste((20, 20, 20, 255), (10, 10, 50, 60))
    im.paste((200, 80, 80, 255), (11, 11, 49, 59))
    for v in DIRS:
        im.save(d / f"{v}_repos.png")
        moved = Image.new("RGBA", (64, 64))
        moved.paste(im, (2, 0))
        for f in WALK:
            moved.save(d / f"{v}_{f}.png")
    good = check(d, {"brief": {"posture": "glisse"}}, legs=False)
    assert good["note"] == 5, good
    bad = im.copy()
    for x in range(0, 40, 4):
        bad.putpixel((55 + x % 8, 2 + x), (200, 80, 80, 255))
    for f in WALK:
        bad.save(d / f"gauche_{f}.png")
        im.save(d / f"bas_{f}.png")
    r = check(d, {"brief": {}, "lore": {"poids": "2 tonnes", "combat": {"stats": {"pv": 20, "defense": 20, "vitesse": 90},
                                                                           "attaques": [dict(nom="Griffe de Vent", genre="pluie")]}}}, legs=False)
    codes = {p["code"] for p in r["problemes"]}
    assert {"miettes", "attaque", "stats"} <= codes and r["note"] < 5, r
    torn = im.copy()
    torn.paste((0, 0, 0, 0), (30, 30, 50, 34))  # une entaille : l'intérieur rouge est à nu
    assert outline_breaks(close_outline(torn, im), im) < outline_breaks(torn, im), "close_outline"
    assert crumbs(bad, im) == 10 and body_class({"taille": "30 cm"}) == "legere"
    print("ok", r["note"], sorted(codes))
