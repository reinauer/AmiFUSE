"""
LRU cache for converted Amiga icons.

This module provides thread-safe caching of converted icon data (ICNS format)
to avoid repeatedly parsing and converting .info files.
"""

import threading
import time
from typing import Dict, Optional, Tuple


class IconCache:
    """Thread-safe LRU cache for converted icon data."""

    def __init__(self, max_entries: int = 500, max_memory_mb: int = 50):
        """Initialize the icon cache.

        Args:
            max_entries: Maximum number of cached icons.
            max_memory_mb: Maximum memory usage in megabytes.
        """
        self._cache: Dict[str, Tuple[bytes, float]] = {}  # path -> (icns_data, access_time)
        self._max_entries = max_entries
        self._max_memory = max_memory_mb * 1024 * 1024
        self._current_memory = 0
        self._lock = threading.Lock()

    def get(self, path: str) -> Optional[bytes]:
        """Get cached ICNS data for a path.

        Args:
            path: The .info file path.

        Returns:
            Cached ICNS bytes, or None if not cached.
        """
        with self._lock:
            entry = self._cache.get(path)
            if entry is None:
                return None
            icns_data, _ = entry
            # Update access time
            self._cache[path] = (icns_data, time.time())
            return icns_data

    def put(self, path: str, icns_data: bytes) -> None:
        """Cache ICNS data for a path.

        Args:
            path: The .info file path.
            icns_data: The converted ICNS data.
        """
        with self._lock:
            # Remove old entry if exists
            if path in self._cache:
                old_data, _ = self._cache[path]
                self._current_memory -= len(old_data)

            # Evict if needed
            self._evict_if_needed(len(icns_data))

            # Add new entry
            self._cache[path] = (icns_data, time.time())
            self._current_memory += len(icns_data)

    def invalidate(self, path: str) -> None:
        """Remove a path from the cache.

        Args:
            path: The .info file path to invalidate.
        """
        with self._lock:
            if path in self._cache:
                data, _ = self._cache[path]
                self._current_memory -= len(data)
                del self._cache[path]

    def clear(self) -> None:
        """Clear all cached entries."""
        with self._lock:
            self._cache.clear()
            self._current_memory = 0

    def _evict_if_needed(self, new_size: int) -> None:
        """Evict oldest entries if cache is over limits.

        Must be called with lock held.
        """
        # Check if we need to evict
        while (len(self._cache) >= self._max_entries or
               self._current_memory + new_size > self._max_memory):
            if not self._cache:
                break

            # Find oldest entry
            oldest_path = None
            oldest_time = float('inf')
            for path, (_, access_time) in self._cache.items():
                if access_time < oldest_time:
                    oldest_time = access_time
                    oldest_path = path

            if oldest_path:
                data, _ = self._cache[oldest_path]
                self._current_memory -= len(data)
                del self._cache[oldest_path]

    @property
    def size(self) -> int:
        """Return the number of cached entries."""
        with self._lock:
            return len(self._cache)

    @property
    def memory_usage(self) -> int:
        """Return current memory usage in bytes."""
        with self._lock:
            return self._current_memory


class IconExistenceCache:
    """Cache for tracking which files have valid .info icon files."""

    def __init__(self, ttl_seconds: float = 3600.0):
        """Initialize the existence cache.

        Args:
            ttl_seconds: Time-to-live for cache entries in seconds.
        """
        self._cache: Dict[str, Tuple[bool, float]] = {}  # path -> (has_icon, timestamp)
        self._ttl = ttl_seconds
        self._lock = threading.Lock()

    def get(self, path: str) -> Optional[bool]:
        """Check if we have cached knowledge about a path's icon.

        Args:
            path: The file path (not the .info path).

        Returns:
            True if file has a valid icon, False if it doesn't, None if unknown.
        """
        with self._lock:
            entry = self._cache.get(path)
            if entry is None:
                return None
            has_icon, timestamp = entry
            if time.time() - timestamp > self._ttl:
                del self._cache[path]
                return None
            return has_icon

    def put(self, path: str, has_icon: bool) -> None:
        """Cache whether a path has a valid icon.

        Args:
            path: The file path (not the .info path).
            has_icon: Whether the file has a valid .info icon.
        """
        with self._lock:
            self._cache[path] = (has_icon, time.time())

    def invalidate(self, path: str) -> None:
        """Remove a path from the cache."""
        with self._lock:
            self._cache.pop(path, None)

    def clear(self) -> None:
        """Clear all cached entries."""
        with self._lock:
            self._cache.clear()
