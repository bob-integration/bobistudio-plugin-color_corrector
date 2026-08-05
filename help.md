# Correcteur de couleur

Applique une correction colorimétrique live sur un flux vidéo du pipeline : luminosité, contraste,
gamma, saturation, teinte, gamma par canal R/G/B, color balance 3 zones (ombres/tons moyens/
lumières) et glow/bloom. Presets enregistrables et rappelables à chaud. Réglages neutres =
passthrough (coût quasi nul, pas de conversion RGB).

## Régler la correction

Dans **Traitements → Correcteurs** :

**Base**

| Paramètre | Plage | Effet |
|---|---|---|
| Luminosité | −1 à 1 (déf. 0) | Décalage additif |
| Contraste | 0 à 2 (déf. 1) | Écartement autour du gris moyen |
| Gamma | 0,1 à 10 (déf. 1) | Courbe de luminance globale |
| Saturation | 0 à 3 (déf. 1) | 0 = noir et blanc, 1 = neutre |
| Teinte | −180° à 180° (déf. 0) | Rotation de la teinte |

**Panneau avancé**

| Paramètre | Plage | Effet |
|---|---|---|
| Gamma R / V / B | 0,1 à 10 chacun (déf. 1) | Gamma indépendant par canal — corrige une dominante sans toucher aux autres canaux |
| Balance — Ombres / Tons moyens / Lumières | −1 à 1 par canal R/V/B (déf. 0) | Grading 3 zones (comme un étalonneur) : chaque zone pondère son influence selon la luminance du pixel (poids continus, pas de coupure nette entre zones) |
| Glow / Bloom | actif (case) + intensité 0-2, seuil 0-1 (déf. 0,7), rayon 1-64 px (déf. 8) | Diffusion lumineuse autour des hautes lumières au-dessus du seuil. La case **actif** coupe l'effet sans perdre le réglage d'intensité (pratique pour comparer avant/après sans tout ressaisir) |

Tous les réglages s'appliquent **en temps réel**, sans redéploiement.

## Ce que ça coûte

- Luminosité/contraste/gamma (simple)/saturation/teinte passent par des tables précalculées :
  coût quasi nul quel que soit le réglage.
- **Gamma par canal R/G/B ou color balance** (dès qu'un des 9 champs de balance est non nul)
  déclenchent un aller-retour YUV→RGB→YUV, plus coûteux — pris en charge par un **noyau C dédié**
  (AVX2). Sans ce noyau (CPU sans AVX2, ou `.so` non chargé), le repli numpy pur de ce chemin
  coûte **175 à 280 ms/trame** — très au-dessus du budget d'une trame — et le journal du
  conteneur le signale (« noyau cc_full indisponible… »). En pratique : sur un nœud sans AVX2,
  éviter le gamma par canal et la color balance, ou vérifier `GET :8082/state` pour confirmer que
  le noyau est actif avant de s'en servir en direct.
- Le **glow** est un flou plein champ : coût proportionnel au rayon, actif seulement si la case
  est cochée ET l'intensité > 0.

## Presets

Enregistrer une correction (bouton « Enregistrer ») pour la rappeler instantanément. Les presets
sont **globaux et nommés de façon unique** (partagés entre toutes les instances du correcteur, pas
propres à un conteneur) et rappelables depuis les macros/shotbox. Le bouton **Réinitialiser**
remet tous les paramètres à leur valeur neutre (passthrough).

## Câblage

Entrée : 1 flux vidéo (câblage à chaud, page **Câbles**). Sortie : 1 flux vidéo corrigé
(`<hostname>_cc`). Le format d'entrée (résolution, cadence, chroma, profondeur) est repris
automatiquement — rien à configurer côté format.

## Mode tranche (latence réduite)

Le correcteur peut publier sa sortie **bande par bande** au fil de l'arrivée de la source, au lieu
d'attendre la trame complète (mode tranche MXL) : l'étage correcteur n'ajoute alors plus ~1 image
de latence de traversée.

- **Opt-in** : paramètre `slice_mode` au déploiement (désactivé par défaut — comportement
  historique strictement inchangé).
- Conditions : genlock actif (verrou-entrée 1:1) et source progressive ; en entrelacé ou genlock
  désactivé, le chemin classique s'applique — le plugin le signale dans le journal si le mode a
  été demandé mais n'a pas pu s'activer.
- Le rendu bandé est identique au rendu plein (octet pour octet).
- Cas particulier : quand le **glow** est actif (flou plein champ, non bandable), la sortie n'est
  publiée qu'en fin d'image — l'aval ne voit jamais un rendu intermédiaire sans glow.

## Diagnostic

- `GET :8082/state` publie le format courant (résolution/chroma/profondeur), les paramètres actifs,
  l'état du mode tranche effectif, et si le noyau C accéléré (`cc_full`) a pu être chargé —
  premier réflexe si la correction semble trop lente ou n'apparaît pas.
- Une perte de source déclenche une réouverture automatique du flux MXL (SIGBUS intercepté) —
  aucune action opérateur n'est nécessaire, un log « SIGBUS reçu » en garde la trace.

## Notes

- Le correcteur ne touche ni à la résolution ni à la cadence.
- Réglages neutres = passthrough (coût quasi nul).
