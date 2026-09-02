# Color corrector

*[English version](README.en.md)*

Corrige les couleurs d'un flux vidéo du bus MXL. Il fait partie de
[Bobi.Studio](https://github.com/bob-integration/bobistudio), un orchestrateur broadcast bâti sur
le bus ST 2110 / MXL.

Une entrée, une sortie, et des réglages qui s'appliquent **à chaud** — sans redéploiement, sans
coupure d'image.

---

## Les réglages

**Luminance**

| | plage | défaut | |
|---|---|---|---|
| Luminosité | −1 → +1 | 0 | décalage additif |
| Contraste | 0 → 2 | 1 | facteur autour du gris moyen |
| Gamma | 0,1 → 10 | 1 | exposant |

**Couleur**

| | plage | défaut | |
|---|---|---|---|
| Saturation | 0 → 3 | 1 | 0 rend une image grise |
| Teinte | −180° → +180° | 0° | rotation dans le plan chroma |

**Gamma par canal** — un exposant par composante, `gamma_r`, `gamma_g`, `gamma_b`, de 0,1 à 10.
C'est le réglage qui rattrape une dominante qui ne se corrige pas par un simple décalage : une
source dont le vert monte trop vite dans les tons clairs, par exemple.

**Balance** — neuf réglages, trois canaux × trois zones de luminance :

| | rouge | vert | bleu |
|---|---|---|---|
| Ombres | `cb_rs` | `cb_gs` | `cb_bs` |
| Tons moyens | `cb_rm` | `cb_gm` | `cb_bm` |
| Lumières | `cb_rh` | `cb_gh` | `cb_bh` |

Chacun va de −1 à +1. La balance ajoute un **décalage RGB pondéré par la luminance** : les poids
des trois zones se recouvrent en triangle, si bien qu'un pixel sombre reçoit surtout le réglage
« ombres », un pixel clair surtout celui des « lumières », et les valeurs intermédiaires un
mélange continu des trois. Réchauffer les ombres sans toucher aux hautes lumières, ou l'inverse,
tient donc en un seul réglage.

> Cette balance est calculée en **trois tables indexées par la luma**, sans aller-retour
> YUV↔RGB : le chroma s'annule dans la conversion, donc le décalage ne dépend que de Y. La version
> qui faisait l'aller-retour déformait l'image d'environ ±12 sur Y — le raccourci n'est pas
> seulement plus rapide, il est plus juste. Le gamma par canal, lui, garde son aller-retour :
> c'est une opération non-linéaire, elle n'a pas d'équivalent en table sur Y seul.

---

## Ce que ça coûte à la chaîne

Le correcteur travaille en **mode tranche** : il lit son entrée par bandes et publie la sortie au
fur et à mesure, en suivant les tranches du grain source. La correction étant **ligne-locale** —
chaque pixel de sortie ne dépend que du pixel d'entrée correspondant — le résultat est
**octet-identique** à celui d'un traitement en image entière.

C'est ce qui permet de l'insérer sans que la chaîne y perde une image. Un étage qui attend la
trame complète en coûte une à tout ce qui le traverse, et cette dette n'apparaît sur aucun
compteur : l'étage affiche une cadence parfaite.

Trois cas retombent sur le traitement en image entière, à dessein : une entrée **entrelacée**,
une entrée **sans verrou de cadence**, et une **hauteur qui ne se découpe pas** proprement en
bandes. Le repli est journalisé à chaque fois — il ne se devine pas.

---

## Le piloter

Tous les réglages sont exposés aux **macros et déclencheurs** de Bobi.Studio, avec leurs bornes :
un contrôleur, une surface de contrôle ou un automatisme les atteint sans passer par l'interface.

Le conteneur publie son état et ses métriques sur `:8080`, et accepte les réglages sur `:8082`.

---

## L'installer

**Depuis Bobi.Studio** — page **Catalogue**, qui liste les composants publiés et les installe. Ou
Réglages → Plugins → *Importer*, avec un paquet `.mxlplugin`.

**À la main** — clonez ce dépôt dans `plugins/color_corrector/` d'une instance, puis rechargez le
registre des plugins.

---

## Le lire

- `script.py` — le plugin, un gabarit `str.format` rendu par l'orchestrateur et exécuté dans le
  conteneur. **Toute accolade littérale y est doublée `{{ }}`**, commentaires compris.
- `control.js` / `control.html` / `control.css` — la console de réglage.
- `plugin.json` — câblage, schéma de configuration, surface de macros, points de contrôle.
- `meta.json` — le journal des versions : ce qui cassait, ce qui a été mesuré, ce que la
  correction a coûté.
- `help.md` — l'article que la page Aide du produit construit depuis ce plugin.

---

## Licence

GPL-3.0-or-later — voir [LICENSE](LICENSE). Copyright © 2026 BOBI SAS, France.
