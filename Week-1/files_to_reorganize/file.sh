#!/bin/bash

# Process all .txt files safely (handles spaces & special characters)
find . -type f -name "*.txt" -print0 | while IFS= read -r -d '' file
do
    # Extract category from FIRST matching line
    category=$(grep -m 1 "^category:" "$file" | cut -d' ' -f2- | tr -d '\r')

    # Skip if no category found
    [ -z "$category" ] && continue

    # Create category directory
    mkdir -p "$category"

    # Remove leading ./ from path
    relpath="${file#./}"

    # Convert full path to dash format
    newname=$(printf '%s' "$relpath" | tr '/' '-')

    # Move file
    mv -- "$file" "$category/$newname"

done
