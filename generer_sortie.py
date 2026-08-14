"""Génère la bande rythmo d'une vidéo de videos/ et range la sortie dans sorties/.

Usage :  .venv/Scripts/python generer_sortie.py [chemin_video] [options_json]
Exemple : .venv/Scripts/python generer_sortie.py videos/Redoublage.mp4
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

VIDEO = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("videos/Redoublage.mp4")
OPTIONS = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {
    "modele": "base", "lipsync": True, "diariser": True, "asr": "local",
}
SORTIES = Path("sorties")


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "backend"))
    from app.pipeline import traiter_job

    if not VIDEO.is_file():
        print(f"Vidéo introuvable : {VIDEO}")
        return 1

    job_dir = SORTIES / VIDEO.stem
    job_dir.mkdir(parents=True, exist_ok=True)

    def progresser(p, s):
        print(f"  {p:3d} % — {s}")

    print(f"Traitement de {VIDEO} (options : {OPTIONS})…")
    final = traiter_job(job_dir, VIDEO, OPTIONS, progresser)
    if final is None:  # edition=True → pas de rendu direct
        print("Pause édition : relancez avec edition=False pour le rendu.")
        return 0

    final = Path(final)
    copie = SORTIES / f"{VIDEO.stem}_bande_rythmo.mp4"
    import shutil
    shutil.copyfile(final, copie)

    from app.edition import lire_repliques
    payload = lire_repliques(job_dir)
    (SORTIES / f"{VIDEO.stem}.srt").write_text(
        __import__("app.srt_export", fromlist=["generer_srt"]).generer_srt(payload),
        encoding="utf-8")
    print(f"\n[OK] MP4 final : {copie.resolve()}")
    print(f"   (source {VIDEO} : {len(payload['repliques'])} répliques, "
          f"{payload.get('nb_personnages', '?')} voix)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
