"""Former name of shim_cli.hook. Kept so a 0.2.0 hook command keeps working."""

from shim_cli.hook import main

__all__ = ["main"]

if __name__ == "__main__":
    main()
