# Third-party notices

SnekBrain's own code is MIT licensed (see LICENSE). The Windows installer also contains these
components, each under its own license. Their license files are kept inside the install folder
(`pkgs\<name>.dist-info` and `Python\LICENSE.txt`).

| Component | Used for | License |
|---|---|---|
| Python (embeddable build) | runs the app | PSF License |
| PyTorch | the neural network and its training | BSD-3-Clause style, with bundled parts under Apache-2.0, MIT, BSD-2-Clause, BSL-1.0, ISC and Zlib (see its dist-info) |
| Stable-Baselines3 | the PPO training algorithm | MIT |
| Gymnasium | the game environment interface | MIT |
| NumPy | numbers and the game simulation | BSD-3-Clause, with bundled parts under 0BSD, MIT, Zlib and CC0-1.0 (see its dist-info) |
| cloudpickle | used by Stable-Baselines3 | BSD-3-Clause |
| psutil | CPU limits, pausing during videos | BSD-3-Clause |
| winrt (pywinrt) | seeing when a video is playing | MIT |
| filelock, fsspec, Jinja2, MarkupSafe, networkx, setuptools, sympy, mpmath, typing-extensions, Farama-Notifications | PyTorch / Gymnasium dependencies | MIT / BSD-3-Clause / BSD-3-Clause / BSD-3-Clause / BSD-3-Clause / MIT / BSD / BSD-3-Clause / PSF-2.0 / MIT |
| Microsoft Visual C++ runtime (msvcp140 and related DLLs) | needed by PyTorch | Microsoft Visual C++ Redistributable license (distributable code) |
| NSIS | the installer itself | zlib/libpng license |

The optional GPU add-on downloads NVIDIA's CUDA build of PyTorch from download.pytorch.org at the
user's request; it includes NVIDIA CUDA libraries under NVIDIA's license terms.

The snake game, its art and the SnekBrain name and icon are original work by Terrelbytes AI Solutions.
