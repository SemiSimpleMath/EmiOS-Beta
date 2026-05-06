#!/usr/bin/env python3
"""
EmiAi Setup Script
Initializes the application for first-time use
"""

import sys
import subprocess
import os
import struct
import math
from pathlib import Path

def print_header(text):
    """Print a formatted header"""
    print("\n" + "="*80)
    print(f"  {text}")
    print("="*80)

def print_step(step_num, total_steps, text):
    """Print a formatted step"""
    print(f"\n[{step_num}/{total_steps}] {text}")

def check_python_version():
    """Check if Python version is 3.10 or 3.11 (ChromaDB ceiling)"""
    print_step(1, 8, "Checking Python version...")

    version = sys.version_info
    if version < (3, 10):
        print(f"❌ ERROR: Python 3.10 or 3.11 required, you have {version.major}.{version.minor}")
        print("   Install Python 3.11 from python.org and rerun via:  py -3.11 setup.py")
        return False
    if version >= (3, 12):
        print(f"❌ ERROR: Python {version.major}.{version.minor} is not supported.")
        print("   ChromaDB (the embeddings backend) does not ship wheels for Python 3.12+.")
        print("   Install Python 3.11 from python.org and rerun via:  py -3.11 setup.py")
        return False

    print(f"✅ Python {version.major}.{version.minor}.{version.micro} detected")
    return True

def create_virtual_environment():
    """Create a Python virtual environment"""
    print_step(2, 8, "Creating virtual environment...")
    
    venv_path = Path(".venv")
    if venv_path.exists():
        print("⚠️  Virtual environment already exists, skipping...")
        return True
    
    try:
        subprocess.run([sys.executable, "-m", "venv", ".venv"], check=True)
        print("✅ Virtual environment created at .venv/")
        
        # Ensure pip is available in the venv
        python_cmd = get_python_command()
        try:
            print("   Ensuring pip is available...")
            subprocess.run([python_cmd, "-m", "ensurepip", "--default-pip"], check=False)
            print("   ✅ pip is ready")
        except Exception as e:
            print(f"   ⚠️  Note: {e}")
        
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ ERROR: Failed to create virtual environment: {e}")
        return False

def get_pip_command():
    """Get the pip command for the current OS"""
    if sys.platform == "win32":
        return str(Path(".venv") / "Scripts" / "pip.exe")
    else:
        return str(Path(".venv") / "bin" / "pip")

def install_dependencies():
    """Install Python dependencies"""
    print_step(3, 8, "Installing dependencies...")
    
    pip_cmd = get_pip_command()
    python_cmd = get_python_command()
    
    # Try to upgrade pip (non-critical)
    try:
        print("\n   Upgrading pip...")
        subprocess.run([pip_cmd, "install", "--upgrade", "pip"], check=False)
    except Exception as e:
        print(f"   ⚠️  Could not upgrade pip (non-critical): {e}")
    
    # Install requirements (critical)
    try:
        print("\n   Installing packages from requirements_alpha.txt...")
        subprocess.run([pip_cmd, "install", "-r", "requirements_alpha.txt"], check=True)
        print("✅ All dependencies installed")
    except subprocess.CalledProcessError as e:
        print(f"❌ ERROR: Failed to install dependencies: {e}")
        return False
    
    print("\n   Knowledge Graph enabled (ONNX embeddings, no PyTorch needed)")
    return True

def check_credentials():
    """Check if required credential files exist"""
    print_step(4, 8, "Checking credentials...")
    
    creds_dir = Path("app/assistant/lib/credentials")
    required_files = {
        "credentials.json": "Google API credentials (for Calendar/Gmail)",
        "token.pickle": "Google API token (will be created on first auth)"
    }
    
    missing = []
    for file, desc in required_files.items():
        file_path = creds_dir / file
        if not file_path.exists():
            if file == "token.pickle":
                print(f"⚠️  {file} - {desc} (will be created when you authorize)")
            else:
                print(f"❌ MISSING: {file} - {desc}")
                missing.append(file)
        else:
            print(f"✅ {file} found")
    
    if missing:
        print("\n⚠️  WARNING: Some credentials are missing.")
        print("   You'll need to provide these before using Calendar/Gmail features.")
        print("   See BETA.md for setup instructions.")
    
    return True  # Not critical, just warn

def create_directories():
    """Create necessary directories"""
    print_step(5, 8, "Creating directories...")
    
    directories = [
        "logs",
        "uploads",
        "instance",
        "app/personal_info",
        "chroma_db"
    ]
    
    for dir_path in directories:
        Path(dir_path).mkdir(parents=True, exist_ok=True)
        print(f"✅ Created {dir_path}/")
    
    return True

def get_python_command():
    """Get the Python command for the current OS from venv"""
    if sys.platform == "win32":
        return str(Path(".venv") / "Scripts" / "python.exe")
    else:
        return str(Path(".venv") / "bin" / "python")

def initialize_database():
    """Initialize the database tables"""
    print_step(6, 8, "Initializing database...")
    
    try:
        python_cmd = get_python_command()
        
        init_script = """
import sys, os
sys.path.insert(0, '.')

from dotenv import load_dotenv
load_dotenv()

print("   Creating database tables (standalone, no Flask app needed)...")

from app.models.base import Base, get_current_engine
engine = get_current_engine()

# Create all tables registered on Base.metadata (core + entity cards + news)
Base.metadata.create_all(engine, checkfirst=True)
print("   Core tables created.")

# Music tables use Flask-SQLAlchemy metadata — create them directly with the engine
from app.assistant.database.db_instance import db
from app.models.played_songs import PlayedSong
from app.models.music_tracks_spotify import SpotifyMusicTrack
from app.models.music_genre_stats import MusicGenreStats
from app.models.music_weights import MusicGenreWeight, MusicArtistWeight, MusicTrackWeight

db.Model.metadata.create_all(
    engine,
    tables=[
        PlayedSong.__table__,
        SpotifyMusicTrack.__table__,
        MusicGenreStats.__table__,
        MusicGenreWeight.__table__,
        MusicArtistWeight.__table__,
        MusicTrackWeight.__table__,
    ],
    checkfirst=True,
)
print("   Music tables created.")

# KG tables — import every database module so all model classes register
# with Base.metadata before create_all is called.
try:
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node, Edge
    from app.assistant.kg_core.taxonomy.models import Taxonomy, NodeTaxonomyLink
    from app.assistant.database import claim_proposals  # noqa: F401
    from app.assistant.database import kg_chat_projection  # noqa: F401
    from app.assistant.database import kg_maintenance_finding  # noqa: F401
    from app.assistant.database import kg_merge_log  # noqa: F401
    from app.assistant.database import kg_pipeline_models  # noqa: F401
    from app.assistant.database import kg_revision_log  # noqa: F401
    from app.assistant.database import entity_card_maintenance_finding  # noqa: F401
    from app.assistant.database import processed_entity_log  # noqa: F401
    Base.metadata.create_all(engine, checkfirst=True)
    print("   Knowledge Graph tables created.")
except Exception as e:
    print(f"   KG tables skipped: {e}")

# Belief engine tables (separate schema, raw SQL bootstrap)
try:
    from belief_engine.db.ensure_schema import ensure_schema as _ensure_belief_schema
    _ensure_belief_schema()
    print("   Belief engine tables created.")
except Exception as e:
    print(f"   Belief engine tables skipped: {e}")
"""
        
        # Run using venv Python
        result = subprocess.run(
            [python_cmd, "-c", init_script],
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            check=True
        )
        
        if result.stdout:
            print(result.stdout)
        
        print("✅ Database tables initialized")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ ERROR: Failed to initialize database: {e}")
        if e.stdout:
            print("STDOUT:", e.stdout)
        if e.stderr:
            print("STDERR:", e.stderr)
        return False
    except Exception as e:
        print(f"❌ ERROR: Failed to initialize database: {e}")
        import traceback
        traceback.print_exc()
        return False

def create_default_resources():
    """Create default resource files if they don't exist"""
    print_step(7, 8, "Checking resource files...")
    
    resources_dir = Path("resources")
    
    # Check if critical resource files exist
    required_resources = [
        "resource_user_data.json",
        "resource_assistant_data.json",
        "resource_assistant_personality_data.json",
        "resource_chat_guidelines_data.json"
    ]
    
    missing = []
    for resource in required_resources:
        if not (resources_dir / resource).exists():
            missing.append(resource)
    
    if missing:
        print(f"\n⚠️  WARNING: Missing resource files:")
        for res in missing:
            print(f"   - {res}")
        print("\n   You'll need to run the setup wizard on first launch.")
        print("   Navigate to http://localhost:8000/setup")
    else:
        print("✅ All resource files present")
    
    return True

def _generate_ico(ico_path):
    """Generate a 32x32 .ico with the EmiAi logo (teal circle + white E)."""
    SIZE = 32
    CX, CY, R = 15.5, 15.5, 15.5
    TEAL_BGRA = (177, 197, 78, 255)
    WHITE_BGRA = (255, 255, 255, 255)

    def _in_e(x, yt):
        return (
            (8 <= yt <= 9 and 11 <= x <= 20)
            or (22 <= yt <= 23 and 11 <= x <= 20)
            or (15 <= yt <= 16 and 11 <= x <= 18)
            or (8 <= yt <= 23 and 11 <= x <= 12)
        )

    pixels = bytearray()
    and_mask = bytearray()

    for row in range(SIZE):
        yt = SIZE - 1 - row
        mask_byte = 0
        for x in range(SIZE):
            dist = math.hypot(x - CX, yt - CY)
            if dist <= R - 0.5:
                alpha = 255
            elif dist <= R + 0.5:
                alpha = int(255 * (R + 0.5 - dist))
            else:
                alpha = 0

            if alpha > 0:
                if _in_e(x, yt):
                    b, g, r = WHITE_BGRA[:3]
                else:
                    b, g, r = TEAL_BGRA[:3]
                pixels.extend((b, g, r, alpha))
            else:
                pixels.extend((0, 0, 0, 0))
                mask_byte |= 1 << (7 - (x % 8))

            if x % 8 == 7:
                and_mask.append(mask_byte)
                mask_byte = 0

    bmp_header = struct.pack(
        '<IiiHHIIiiII',
        40, SIZE, SIZE * 2, 1, 32, 0, 0, 0, 0, 0, 0,
    )
    image_data = bmp_header + bytes(pixels) + bytes(and_mask)

    ico_header = struct.pack('<HHH', 0, 1, 1)
    image_offset = 6 + 16
    dir_entry = struct.pack(
        '<BBBBHHII',
        SIZE, SIZE, 0, 0, 1, 32, len(image_data), image_offset,
    )

    p = Path(ico_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'wb') as f:
        f.write(ico_header)
        f.write(dir_entry)
        f.write(image_data)


def create_desktop_shortcut():
    """Create a desktop shortcut pointing to the EmiAi launcher."""
    print_step(8, 8, "Creating desktop shortcut...")

    project_root = Path.cwd()
    ico_path = project_root / "app" / "static" / "images" / "emi.ico"

    if not ico_path.exists():
        try:
            _generate_ico(ico_path)
            print("   Generated app icon (emi.ico)")
        except Exception as e:
            print(f"   ⚠️  Could not generate icon: {e}")

    desktop = Path.home() / "Desktop"
    if not desktop.exists():
        print("⚠️  Desktop folder not found, skipping shortcut.")
        return True

    if sys.platform == "win32":
        launcher = project_root / "emi.bat"
        if not launcher.exists():
            print("⚠️  emi.bat not found, skipping shortcut.")
            return True

        lnk_path = desktop / "EmiAi.lnk"
        icon_arg = f"$s.IconLocation = '{ico_path}';" if ico_path.exists() else ""
        ps_script = (
            "$ws = New-Object -ComObject WScript.Shell;"
            f"$s = $ws.CreateShortcut('{lnk_path}');"
            f"$s.TargetPath = '{launcher}';"
            f"$s.WorkingDirectory = '{project_root}';"
            f"{icon_arg}"
            "$s.Description = 'Launch EmiAi';"
            "$s.Save();"
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                check=True,
                capture_output=True,
            )
            print(f"✅ Desktop shortcut created: {lnk_path}")
        except subprocess.CalledProcessError as e:
            print(f"⚠️  Could not create shortcut: {e.stderr or e}")
    else:
        launcher = project_root / "emi.command"
        if not launcher.exists():
            print("⚠️  emi.command not found, skipping shortcut.")
            return True

        link = desktop / "EmiAi"
        try:
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to(launcher)
            os.chmod(launcher, 0o755)
            print(f"✅ Desktop shortcut created: {link}")
        except Exception as e:
            print(f"⚠️  Could not create shortcut: {e}")

    return True


def print_next_steps():
    """Print next steps for the user"""
    print_header("SETUP COMPLETE!")
    
    print("\nNext Steps:\n")

    print("1. Double-click the EmiAi shortcut on your Desktop.")
    print("   (If the server is already running, it just opens the browser.)")
    print()
    print("   Alternatively, from the terminal:")
    if sys.platform == "win32":
        print("   .\\emi.bat")
    else:
        print("   ./emi.command")
    print()
    print("2. On first launch, the setup wizard will guide you through:")
    print("   - Your profile and preferences")
    print("   - Choosing an AI provider (OpenAI, Gemini, or Claude)")
    print("   - Naming and personalising your assistant")
    print()
    print("Documentation:")
    print("   - First-time install + first week: BETA.md")
    print("   - Architecture & dev docs: docs/")
    print()

def main():
    """Main setup function"""
    print_header("🚀 EmiAi Setup")
    print("\nThis script will set up your EmiAi environment.\n")
    
    # Change to script directory
    os.chdir(Path(__file__).parent)
    
    steps = [
        ("Checking Python version", check_python_version),
        ("Creating virtual environment", create_virtual_environment),
        ("Installing dependencies", install_dependencies),
        ("Checking credentials", check_credentials),
        ("Creating directories", create_directories),
        ("Initializing database", initialize_database),
        ("Checking resources", create_default_resources),
        ("Creating desktop shortcut", create_desktop_shortcut)
    ]
    
    failed = []
    
    for step_name, step_func in steps:
        try:
            if not step_func():
                failed.append(step_name)
        except Exception as e:
            print(f"\n❌ ERROR in {step_name}: {e}")
            import traceback
            traceback.print_exc()
            failed.append(step_name)
    
    if failed:
        print_header("❌ SETUP INCOMPLETE")
        print("\nThe following steps failed:")
        for step in failed:
            print(f"   ❌ {step}")
        print("\nPlease fix the errors and run setup.py again.")
        return 1
    
    print_next_steps()
    return 0

if __name__ == "__main__":
    sys.exit(main())

