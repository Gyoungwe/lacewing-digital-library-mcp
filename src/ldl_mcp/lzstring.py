"""Minimal LZ-String URI codec compatible with lz-string 1.5.0.

Adapted from py-lzstring 0.1.1 and lz-string, both distributed under MIT.
Only the URI-component pair needed by the LDL state parameter is included.
"""

from __future__ import annotations

from collections.abc import Callable


_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-$"
_REVERSE = {character: index for index, character in enumerate(_ALPHABET)}


class _BitWriter:
    __slots__ = ("bits_per_char", "emit", "value", "position")

    def __init__(self, bits_per_char: int, emit: Callable[[int], None]) -> None:
        self.bits_per_char = bits_per_char
        self.emit = emit
        self.value = 0
        self.position = 0

    def write(self, value: int, bit_count: int) -> None:
        for _ in range(bit_count):
            self.value = (self.value << 1) | (value & 1)
            value >>= 1
            self.position += 1
            if self.position == self.bits_per_char:
                self.emit(self.value)
                self.value = 0
                self.position = 0

    def flush(self) -> None:
        while True:
            self.value <<= 1
            if self.position == self.bits_per_char - 1:
                self.emit(self.value)
                self.value = 0
                self.position = 0
                return
            self.position += 1


class _BitReader:
    __slots__ = ("get_value", "reset_value", "value", "position", "index")

    def __init__(self, get_value: Callable[[int], int], reset_value: int) -> None:
        self.get_value = get_value
        self.reset_value = reset_value
        self.value = 0
        self.position = 0
        self.index = 0

    def read(self, bit_count: int) -> int:
        result = 0
        power = 1
        while power != 1 << bit_count:
            if self.position == 0:
                self.position = self.reset_value
                self.value = self.get_value(self.index)
                self.index += 1
            if self.value & self.position:
                result |= power
            self.position >>= 1
            power <<= 1
        return result


def compress_to_encoded_uri_component(text: str | None) -> str:
    if text is None:
        return ""

    output: list[str] = []
    writer = _BitWriter(6, lambda value: output.append(_ALPHABET[value]))
    dictionary: dict[str, int] = {}
    pending: set[str] = set()
    dictionary_size = 3
    bit_count = 2
    enlarge_in = 2
    word = ""

    def enlarge() -> None:
        nonlocal enlarge_in, bit_count
        enlarge_in -= 1
        if enlarge_in == 0:
            enlarge_in = 1 << bit_count
            bit_count += 1

    def write_token(token: str) -> None:
        if token in pending:
            code = ord(token)
            if code < 256:
                writer.write(0, bit_count)
                writer.write(code, 8)
            else:
                writer.write(1, bit_count)
                writer.write(code, 16)
            enlarge()
            pending.discard(token)
        else:
            writer.write(dictionary[token], bit_count)
        enlarge()

    for character in text:
        if character not in dictionary:
            dictionary[character] = dictionary_size
            dictionary_size += 1
            pending.add(character)
        combined = word + character
        if combined in dictionary:
            word = combined
        else:
            write_token(word)
            dictionary[combined] = dictionary_size
            dictionary_size += 1
            word = character

    if word:
        write_token(word)
    writer.write(2, bit_count)
    writer.flush()
    return "".join(output)


def decompress_from_encoded_uri_component(compressed: str | None) -> str | None:
    if compressed is None:
        return ""
    if not compressed:
        return None
    compressed = compressed.replace(" ", "+")
    reader = _BitReader(lambda index: _REVERSE[compressed[index]], 32)
    dictionary = ["", "", ""]
    bit_count = 3
    enlarge_in = 4

    try:
        first_token = reader.read(2)
        if first_token == 0:
            character = chr(reader.read(8))
        elif first_token == 1:
            character = chr(reader.read(16))
        elif first_token == 2:
            return ""
        else:
            return None
    except (IndexError, KeyError):
        return None

    dictionary.append(character)
    result = [character]
    word = character

    while True:
        if reader.index > len(compressed):
            return None
        try:
            token = reader.read(bit_count)
            if token == 0:
                dictionary.append(chr(reader.read(8)))
                token = len(dictionary) - 1
                enlarge_in -= 1
                if enlarge_in == 0:
                    enlarge_in = 1 << bit_count
                    bit_count += 1
            elif token == 1:
                dictionary.append(chr(reader.read(16)))
                token = len(dictionary) - 1
                enlarge_in -= 1
                if enlarge_in == 0:
                    enlarge_in = 1 << bit_count
                    bit_count += 1
            elif token == 2:
                return "".join(result)
        except (IndexError, KeyError):
            return None

        if token < len(dictionary):
            entry = dictionary[token]
        elif token == len(dictionary):
            entry = word + word[0]
        else:
            return None

        result.append(entry)
        dictionary.append(word + entry[0])
        enlarge_in -= 1
        word = entry
        if enlarge_in == 0:
            enlarge_in = 1 << bit_count
            bit_count += 1
