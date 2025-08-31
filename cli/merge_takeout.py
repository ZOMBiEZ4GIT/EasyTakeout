#!/usr/bin/env python3
"""
CLI wrapper for EasyTakeout - Google Takeout Metadata Merger

This script provides a command-line interface for the TakeoutMetadataMergerApp.
"""

import argparse
import sys
import os
from pathlib import Path

# Add the app directory to the path so we can import the main app
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'app'))

try:
    from TakeoutMetadataMergerApp import main as gui_main
except ImportError:
    print("Error: Could not import TakeoutMetadataMergerApp. Make sure it's in the app/ directory.")
    sys.exit(1)


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Merge Google Takeout metadata with your photos and videos",
        prog="merge_takeout"
    )
    
    parser.add_argument(
        "source_dir",
        nargs='?',
        help="Source directory containing Google Takeout files"
    )
    
    parser.add_argument(
        "destination_dir", 
        nargs='?',
        help="Destination directory for processed files"
    )
    
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Launch the GUI application (default if no arguments provided)"
    )
    
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without actually processing files"
    )
    
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose output"
    )
    
    parser.add_argument(
        "--version",
        action="version",
        version="EasyTakeout 1.0.0"
    )
    
    args = parser.parse_args()
    
    # If no arguments or --gui flag, launch the GUI
    if not args.source_dir or args.gui:
        print("Launching GUI application...")
        gui_main()
        return
    
    # Validate arguments for CLI mode
    if not args.destination_dir:
        parser.error("destination_dir is required when not using GUI mode")
    
    source_path = Path(args.source_dir)
    dest_path = Path(args.destination_dir)
    
    if not source_path.exists():
        print(f"Error: Source directory '{source_path}' does not exist.")
        sys.exit(1)
    
    if args.verbose:
        print(f"Source directory: {source_path}")
        print(f"Destination directory: {dest_path}")
        if args.dry_run:
            print("DRY RUN MODE - No files will be modified")
    
    # TODO: Implement CLI processing logic
    print("CLI processing not yet implemented. Use --gui to launch the graphical interface.")
    

if __name__ == "__main__":
    main()
