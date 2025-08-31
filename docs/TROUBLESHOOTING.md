# Troubleshooting Guide

This guide helps you resolve common issues with EasyTakeout.

## Common Issues

### Application Won't Start

**Symptoms**: Application crashes on startup or won't launch

**Solutions**:
1. Ensure you have the latest version of the application
2. Check that your system meets the minimum requirements
3. Try running as administrator (Windows)
4. Check antivirus software isn't blocking the application

### Files Not Processing

**Symptoms**: Processing starts but no files are created

**Possible Causes**:
- Insufficient disk space
- Permission issues with destination folder
- Corrupted source files
- Invalid file paths

**Solutions**:
1. Check available disk space (need at least 2x source size)
2. Verify write permissions to destination folder
3. Try a different destination folder
4. Check source folder contains valid Takeout data

### Slow Processing

**Symptoms**: Processing takes much longer than expected

**Causes & Solutions**:
- **Large files**: Video processing is slower - this is normal
- **Network storage**: Use local drives for better performance
- **Low memory**: Close other applications to free up RAM
- **Background scans**: Pause antivirus real-time scanning temporarily

### Metadata Not Applied

**Symptoms**: Files are copied but don't have correct dates/locations

**Possible Issues**:
1. **Missing JSON files**: Some photos may not have corresponding metadata
2. **Corrupted metadata**: JSON files may be invalid
3. **File format limitations**: Some formats don't support all metadata types

**Solutions**:
- Enable verbose logging to see which files lack metadata
- Check if JSON files exist alongside media files
- Try processing a small subset first to identify issues

### Memory Errors

**Symptoms**: Application crashes with out-of-memory errors

**Solutions**:
1. Process smaller batches of files
2. Close other applications
3. Restart the application between large processing jobs
4. Increase virtual memory (Windows)

## Error Messages

### "Access Denied"
- Check folder permissions
- Run as administrator
- Ensure files aren't open in other applications

### "File Not Found"
- Verify source folder path is correct
- Check if files were moved during processing
- Ensure network drives are connected

### "Invalid Takeout Format"
- Confirm you selected the correct Takeout folder
- Check if ZIP files need to be extracted first
- Verify the folder contains Google Photos data

### "Insufficient Disk Space"
- Free up space on destination drive
- Choose a different destination with more space
- Consider processing in smaller batches

## Getting Help

### Log Files

EasyTakeout creates log files that help diagnose issues:
- Location: `%APPDATA%/EasyTakeout/logs/` (Windows)
- Include these logs when reporting issues

### System Information

When reporting issues, include:
- Operating system version
- Available RAM and disk space
- Approximate number of files being processed
- EasyTakeout version

### Reporting Bugs

1. Check existing issues on GitHub
2. Include log files and error messages
3. Provide steps to reproduce the problem
4. Include system information

## Performance Tips

### Optimal Setup
- Use SSD drives for better performance
- Process on local drives (not network storage)
- Ensure adequate free space (2-3x source size)
- Close unnecessary applications

### Large Libraries
- Process in smaller batches (10,000-50,000 files)
- Use the CLI for automated batch processing
- Consider multiple processing sessions for very large libraries

---

If you can't resolve your issue, please [create an issue on GitHub](https://github.com/yourusername/EasyTakeout/issues) with detailed information about your problem.
