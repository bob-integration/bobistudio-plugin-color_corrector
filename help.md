# Correcteur de couleur

Applique une correction colorimétrique live sur un flux vidéo du pipeline : luminosité, contraste, gamma, saturation, teinte, gamma par canal R/G/B, color balance et glow/bloom. Presets enregistrables et rappelables à chaud.

## Régler la correction

Dans **Traitements → Correcteurs** : ajuster les curseurs de base (luminosité, contraste, gamma, saturation, teinte) et, dans le panneau avancé, le gamma par canal R/G/B, la color balance et le glow/bloom (avec bouton on/off qui conserve le réglage d'intensité). Les modifications sont **appliquées en temps réel** sans redéploiement.

## Presets

Enregistrer une correction (bouton « Enregistrer ») pour la rappeler instantanément. Les presets sont **globaux** (partagés entre les instances du même type) et rappelables depuis les macros/shotbox.

## Câblage

Entrée : 1 flux vidéo. Sortie : 1 flux vidéo corrigé (`<hostname>_cc`). Câbler depuis la page **Câbles**. Le format d'entrée (résolution, cadence, chroma) est repris automatiquement — pas de format à configurer.

## Mode tranche (latence réduite)

Le correcteur peut publier sa sortie **bande par bande** au fil de l'arrivée de la source, au lieu d'attendre la trame complète (mode tranche MXL) : l'étage correcteur n'ajoute alors plus ~1 image de latence de traversée.

- **Opt-in** : paramètre `slice_mode` au déploiement (désactivé par défaut — comportement historique strictement inchangé).
- Conditions : genlock actif et source progressive ; en entrelacé, le chemin classique s'applique.
- Le rendu bandé est identique au rendu plein (octet pour octet).
- Cas particulier : quand le **glow** est actif (effet plein champ), la sortie n'est publiée qu'en fin d'image — l'aval ne voit jamais un rendu intermédiaire sans glow.

## Notes

- Le correcteur ne touche ni à la résolution ni à la cadence
- Réglages neutres = passthrough (coût quasi nul)
