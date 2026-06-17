# main.py
# -*- coding: utf-8 -*-

import sys
import os
import tkinter as tk
from gui import PlaylistUpdaterApp

def print_banner():
    # Banner ASCII con letras claras: L T P F
    banner = r"""
██████╗ ████████╗██████╗ ███████╗
██╔══██╗╚══██╔══╝██╔══██╗██╔════╝
██║  ██║   ██║   ██████╔╝█████╗  
██║  ██║   ██║   ██╔═══╝ ██╔══╝  
██████╔╝   ██║   ██║     ██║     
╚═════╝    ╚═╝   ╚═╝     ╚═╝
        L T P F
  Lost Track Playlist Finder
        By Dj Cacho
"""
    # Asegurar codificación UTF-8 en Windows si hace falta
    try:
        if sys.platform.startswith("win"):
            os.system("")  # habilita ANSI en algunos terminales Windows
        # Imprimir banner
        print(banner)
    except Exception:
        # Fallback simple
        print("L T P F - Lost Track Playlist Finder\n")

if __name__ == "__main__":
    print_banner()
    root = tk.Tk()
    app = PlaylistUpdaterApp(root)
    root.mainloop()
