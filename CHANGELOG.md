# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **VFS metadata serialization**: Switched from pickle to JSON
- **stat() on files without metadata**: Returns synthetic metadata instead of KeyError
- **utime**: Updates VFS metadata instead of silently no-oping
- **readlink**: Validates relative symlink targets stay within sandbox root
- **mkdir mode**: Passes mode argument through to IsolatedFS
- **realpath escape fallback**: Return normalized absolute path instead of "/" when path escapes sandbox

### Changed
- **Metadata caching**: Parsed metadata cached in memory to avoid repeated JSON deserialization
- **remove_many**: Batched metadata update instead of one per file
- **connect_fs removed**: Deferred filesystem config moved to agex where it belongs
- **Backing state ownership**: Documented that VFS backing state should be treated as owned by the instance
