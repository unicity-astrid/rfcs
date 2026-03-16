#!/usr/bin/env python3

"""
Auto-generates the mdBook SUMMARY.md from the text/ directory layout.

Each RFC is a single chapter. If an RFC needs supplementary pages, place them
in a subdirectory matching the RFC filename:

    0001-http-fetch.md
    0001-http-fetch/sequence-diagram.svg

Chapters are presented in sorted order (by RFC number).
"""

import os
import shutil
import subprocess


def main():
    if os.path.exists("src"):
        shutil.rmtree("src")
    os.mkdir("src")

    for path in os.listdir("text"):
        symlink(f"../text/{path}", f"src/{path}")
    symlink("../README.md", "src/introduction.md")

    with open("src/SUMMARY.md", "w") as summary:
        summary.write("[Introduction](introduction.md)\n\n")
        collect(summary, "text", 0)

    subprocess.call(["mdbook", "build"])


def collect(summary, path, depth):
    entries = [e for e in os.scandir(path) if e.name.endswith(".md")]
    entries.sort(key=lambda e: e.name)
    for entry in entries:
        indent = "    " * depth
        name = entry.name[:-3]
        link_path = entry.path[5:]
        summary.write(f"{indent}- [{name}]({link_path})\n")
        maybe_subdir = os.path.join(path, name)
        if os.path.isdir(maybe_subdir):
            collect(summary, maybe_subdir, depth + 1)


def symlink(src, dst):
    if not os.path.exists(dst):
        os.symlink(src, dst)


if __name__ == "__main__":
    main()
