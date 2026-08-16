"""Traduction IA locale pour le doublage (bande rythmo).

Module d'adaptation — pas de traduction littérale — piloté par un LLM 100 %
local, contraint par la durée, les syllabes, les phonèmes et la synchronisation
labiale. La bande rythmo originale n'est jamais modifiée : ce module écrit une
couche séparée ``traduction.json`` dans le dossier du job.
"""
