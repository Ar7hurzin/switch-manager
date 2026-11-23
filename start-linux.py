#!/usr/bin/env python3
"""Compatibilidade com a inicialização anterior no Fedora."""
from start import main
if __name__ == '__main__':
    raise SystemExit(main())
