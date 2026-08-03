# KiLog plugin payload

Copy this directory to the KiCad 9/10 IPC plugin directory, enable the IPC API in
`Preferences > Plugins`, and reload the PCB Editor plugins. The toolbar action launches a small
panel containing `start`, `note`, `undo`, and `end`.

The panel uses the wxPython runtime shipped with KiCad; it does not require Tkinter.

The complete Chinese guide and development notes are available in the project-level `README.md`.
