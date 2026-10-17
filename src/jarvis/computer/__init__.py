"""Computer control: a local vision model looks at the screen and works the mouse and keyboard.

The model (Holo3.1 by H Company, run by llama.cpp on this computer's graphics card) only proposes the next
action. JARVIS decides whether it may happen: every click and keystroke is a `computer_act` call through
the tool executor (kill switch, permissions, risk rules, approval, audit), judged by what JARVIS itself
finds under the pointer or in the focused window (`policy`), never by the model's own description alone.

    service.py   one session at a time, the model server, hiding JARVIS's windows while it works
    session.py   the look → think → act loop, with Esc twice and "the user moved the mouse" as stops
    model.py     the model's tools, prompt and conversation (OpenAI-compatible chat API)
    server.py    starts and stops llama-server for the model
    policy.py    what an action may do: refused, asked first, or just done
    backend.py   screen, windows and input, as an interface (`windows.py` implements it with Win32)
    install.py   downloads and builds everything the model needs (`jarvis computer install`)
"""

from jarvis.computer.backend import ComputerBackend, Shot, Target, get_backend
from jarvis.computer.service import ComputerService
from jarvis.computer.session import Outcome

__all__ = ["ComputerBackend", "ComputerService", "Outcome", "Shot", "Target", "get_backend"]
