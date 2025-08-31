# EasyTakeout User Guide

Welcome to EasyTakeout! This guide will help you get started with merging your Google Takeout metadata with your photos and videos.

## Table of Contents

- [Getting Started](#getting-started)
- [Using the GUI](#using-the-gui)
- [Using the CLI](#using-the-cli)
- [Understanding Google Takeout](#understanding-google-takeout)
- [File Processing](#file-processing)
- [Advanced Features](#advanced-features)
- [FAQ](#faq)

## Getting Started

### Installation

1. Download the latest release from the GitHub releases page
2. Extract the files to your preferred location
3. Run `EasyTakeout.exe` (Windows) or launch the application

### First Steps

1. Export your Google Photos data from [Google Takeout](https://takeout.google.com)
2. Extract the downloaded ZIP files to a folder
3. Launch EasyTakeout
4. Select your Takeout folder as the source
5. Choose a destination folder for processed files
6. Click "Start Processing"

## Using the GUI

### Main Interface

The EasyTakeout GUI provides an intuitive interface for processing your files:

- **Source Folder**: Select the folder containing your Google Takeout data
- **Destination Folder**: Choose where processed files should be saved
- **Processing Options**: Configure how files are handled
- **Progress Tracking**: Monitor the processing status

### Processing Options

- **Merge Metadata**: Combines JSON metadata with media files
- **Preserve Timestamps**: Maintains original creation dates
- **Organize by Date**: Sorts files into date-based folders
- **Skip Duplicates**: Avoids processing duplicate files

## Using the CLI

For advanced users or automation, EasyTakeout includes a command-line interface:

```bash
# Launch GUI
python cli/merge_takeout.py --gui

# Process files via CLI (coming soon)
python cli/merge_takeout.py /path/to/takeout /path/to/output
```

## Understanding Google Takeout

Google Takeout exports your data in a specific format:

- Media files (photos/videos) in various folders
- Corresponding `.json` files with metadata
- Album information and face recognition data
- Location and timestamp information

EasyTakeout intelligently matches these files and merges the metadata.

## File Processing

### What Gets Processed

- **Photos**: JPEG, PNG, HEIC, RAW formats
- **Videos**: MP4, MOV, AVI, and other common formats
- **Metadata**: JSON files with EXIF and location data

### Processing Steps

1. **Discovery**: Scans for media files and their JSON counterparts
2. **Matching**: Pairs files with their metadata
3. **Merging**: Applies metadata to media files
4. **Organization**: Sorts files based on your preferences

## Advanced Features

### Batch Processing

Process multiple Takeout exports simultaneously by selecting multiple source folders.

### Custom Naming

Configure how output files are named:
- Date-based naming
- Original filename preservation
- Custom prefixes and suffixes

### Quality Control

- Duplicate detection and handling
- Corrupted file identification
- Processing verification

## FAQ

### Q: How long does processing take?
A: Processing time depends on the number of files. Expect 1-2 minutes per 1000 files.

### Q: Are my original files modified?
A: No, EasyTakeout creates copies with merged metadata. Your originals remain untouched.

### Q: What if some files don't have JSON metadata?
A: Files without metadata are still copied to maintain your complete library.

### Q: Can I stop and resume processing?
A: Currently, processing must complete in one session. Resume functionality is planned for future releases.

---

For additional help, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md) or file an issue on GitHub.
