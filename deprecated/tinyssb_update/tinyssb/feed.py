import hashlib
import os
import sys
from .packet import Blob, create_apply_pkt, create_upd_pkt
from .packet import Packet
from .packet import PacketType
from .packet import create_chain
from .packet import pkt_from_bytes
from .ssb_util import from_var_int
from .ssb_util import is_file
from .ssb_util import to_hex


# non-micropython import
if sys.implementation.name != "micropython":
    # Optional type annotations are ignored in micropython
    from typing import Optional, List, Tuple, Union


# helper lambdas
slice_type = lambda x: x[7:8]  # for retrieving packet type from wire format
slice_fid = lambda x: x[8:40]  # for retrieving fid contained within payload of packet wire
slice_payload = lambda x: x[8:56]  # for retrieving 48B payload contained in wire format


class Feed:
    """
    Represents a .log file.
    Used to get/append data from/to feeds.
    """

    def __init__(self, file_name: str, skey: Optional[bytes] = None):
        self.file_name = file_name
        self.skey = skey

        # read .log content
        f = open(self.file_name, "rb")
        header = f.read(128)
        f.close()

        # reserved = header[:12]
        self.fid = header[12:44]
        self.parent_id = header[44:76]
        self.parent_seq = int.from_bytes(header[76:80], "big")
        self.anchor_seq = int.from_bytes(header[80:84], "big")
        self.anchor_mid = header[84:104]
        self.front_seq = int.from_bytes(header[104:108], "big")
        self.front_mid = header[108:128]

    def __str__(self) -> str:
        """
        Used for visualizing the current state of the feed in the console.
        """
        title = to_hex(self.fid[:8]) + "..."  # only first 8B of FID
        length = self.front_seq - self.anchor_seq
        separator = ("+-----" * (length + 1)) + "+"
        numbers = "   {}  ".format(self.anchor_seq)
        feed = "| HDR |"

        for i in range(self.anchor_seq + 1, self.front_seq + 1):
            numbers += "   {}  ".format(i)
            pkt_type = self.get_type(i)

            if pkt_type == PacketType.plain48:
                feed += " P48 |"
            if pkt_type == PacketType.chain20:
                feed += " C20 |"
            if pkt_type == PacketType.ischild:
                feed += " ICH |"
            if pkt_type == PacketType.iscontn:
                feed += " ICN |"
            if pkt_type == PacketType.mkchild:
                feed += " MKC |"
            if pkt_type == PacketType.contdas:
                feed += " CTD |"
            if pkt_type == PacketType.updfile:
                feed += " UPD |"
            if pkt_type == PacketType.applyup:
                feed += " APP |"

        return "\n".join([title, numbers, separator, feed, separator])

    def __len__(self) -> int:
        return self.front_seq

    def __getitem__(self, seq: int) -> bytes:
        """
        Returns the payload of the packet with the corresponding
        sequence number.
        Negative indices access the feed starting from the end.
        Also returns full blobs (if complete).
        """
        pkt_wire = self.get_wire(seq)
        pkt_type = pkt_wire[7:8]
        payload = pkt_wire[8:56]

        # check if regular packet or blob
        if pkt_type != PacketType.chain20:
            return payload

        # blob chain
        content_size, num_bytes = from_var_int(payload)
        content = payload[num_bytes:-20]

        # "unwrap" chain
        ptr = payload[-20:]
        while ptr != bytes(20):  # null pointer at end
            blob = self._get_blob(ptr)
            assert blob is not None, "failed to get full blob chain"
            ptr = blob.ptr
            content += blob.payload

        return content[:content_size]  # remove padding

    def __iter__(self):
        self._n = self.anchor_seq
        return self

    def __next__(self) -> bytes:
        self._n += 1
        if self._n > self.front_seq:
            raise StopIteration

        payload = self[self._n]
        return payload

    def get(self, i: int) -> bytes:
        """
        Returns Packet instance with corresponding sequence number in feed.
        Identical to __getitem__.
        """
        return self[i]

    def get_wire(self, seq: int) -> bytes:
        """
        Returns the wire format of the packet with given sequence number.
        """
        # transform negative indices
        if seq < 0:
            seq = self.front_seq + seq + 1  # access last pkt through -1 etc.

        # handle invalid indices
        if seq > self.front_seq or seq <= self.anchor_seq:
            raise IndexError

        # get packet wire
        relative_i = seq - self.anchor_seq  # if seq of first packet is not 1
        f = open(self.file_name, "rb")
        f.seek(128 * relative_i)
        pkt_wire = f.read(128)[8:]  # cut off reserved 8B
        f.close()

        return pkt_wire

    def get_type(self, seq: int) -> Optional[bytes]:
        """
        Returns the type of the packet with given sequence number.
        Bytes can be compared with PacketTypes.
        """
        pkt_wire = self.get_wire(seq)
        pkt_type = pkt_wire[7:8]

        # check if it is a valid packet type
        if PacketType.is_type(pkt_type):
            return pkt_type
        return None

    def get_chain20(self, seq: int) -> Optional[bytes]:
        """
        Returns the ith chain20 packet of the feed.
        Returns None if it does not exist.
        Uses zero-based index.
        Is used in file update feeds (get ith update).
        """
        update_count = 0
        for i in range(self.anchor_seq + 1, self.front_seq + 1):
            if self.get_type(i) == PacketType.chain20:
                if update_count == seq:
                    return self[i]
                else:
                    update_count += 1
        return None

    def count_chain20(self) -> int:
        """
        Returns the number of appended chain20 packets.
        """
        update_count = 0
        for i in range(self.anchor_seq + 1, self.front_seq + 1):
            if self.get_type(i) == PacketType.chain20:
                update_count += 1

        return update_count

    def _update_header(self) -> None:
        """
        Updates the front sequence number and message ID in the .log file
        with the current values of this Feed instance.
        """
        assert type(self.front_mid) is bytes, "front packet is not signed"
        new_info = self.front_seq.to_bytes(4, "big") + self.front_mid
        assert len(new_info) == 24, "new front seq and mid must be 24B"

        # go to beginning of file + 104B (where front seq and mid are)
        # this is not ideal, since the whole file has to be copied to memory
        # this is due to some weird behaviour of micropython on Pycom devices
        # TODO: is there a better solution?
        f = open(self.file_name, "rb+")
        f.seek(0)
        file_content = f.read()
        updated_content = file_content[:104] + new_info + file_content[128:]
        f.seek(0)
        f.write(updated_content)
        f.close()

    def append_pkt(self, pkt: Packet) -> bool:
        """
        Appends given packet to .log file and updates the front sequence number
        and message ID. Returns 'True' on success.
        If the feed has ended (contn), nothing is appended and False is returned.
        """
        if self.has_ended():
            print("cannot append to finished feed")
            return False

        # go to end of buffer and write
        assert pkt.wire is not None, "packet must be signed before appending"
        payload = bytes(8) + pkt.wire  # add reserved 8B
        assert len(payload) == 128, "wire pkt must be 128B"

        f = open(self.file_name, "rb+")
        f.seek(0, 2)  # move to end of file
        f.write(payload)
        f.close()

        # update header info
        self.front_seq += 1
        self.front_mid = pkt.mid
        self._update_header()

        return True

    def append_bytes(self, payload: bytes) -> bool:
        """
        Creates a plain48 packet containing the given payload and appends it
        to the feed. Returns 'True' on success.
        If the feed has ended, nothing is appended and 'False' is returned.
        """
        assert self.front_mid is not None, "front packet must be signed"
        assert self.skey is not None, "can only append if signing key known"

        next_seq = self.front_seq + 1

        pkt = Packet(
            self.fid,
            next_seq.to_bytes(4, "big"),
            self.front_mid,
            payload,
            skey=self.skey,
        )

        return self.append_pkt(pkt)

    def verify_and_append_bytes(self, pkt_wire: bytes) -> bool:
        """
        Creates a new packet from the raw bytes and attempts to validate it.
        Uses the feed_id as the validation key.
        If the packet does not validate, False is returned.
        """
        assert self.front_mid is not None, "front packet must be signed"

        seq = (self.front_seq + 1).to_bytes(4, "big")
        pkt = pkt_from_bytes(self.fid, seq, self.front_mid, pkt_wire)

        if pkt is None:
            # signature verification failed
            return False

        return self.append_pkt(pkt)

    def append_blob(self, payload: bytes) -> bool:
        """
        Creates a blob from the provided payload.
        A packet of type 'chain20' is appended to the feed, referring to the
        blob files (in _blob directory).
        If the feed has ended, nothing is appended and False is returned.
        """
        assert self.front_mid is not None
        assert self.skey is not None, "can only append if signing key is known"

        next_seq = (self.front_seq + 1).to_bytes(4, "big")
        pkt, blobs = create_chain(
            self.fid, next_seq, self.front_mid, payload, self.skey
        )

        if pkt is None:
            return False

        self.append_pkt(pkt)
        return self._write_blob(blobs)

    def _write_blob(self, blobs: List[Blob]) -> bool:
        """
        Takes a list of Blob instances and saves them as blob files
        (as defined in tiny-ssb protocol).
        Returns 'True' on success.
        """
        # get path of _blobs folder
        split = self.file_name.split("/")
        path = "/".join(split[:-2]) + "/_blobs/"

        # first two bytes of hash are the name of the subdirectory
        # ab23e5g... -> _blobs/ab/23e5g...
        for blob in blobs:
            hash_hex = to_hex(blob.signature)
            dir_path = path + hash_hex[:2]
            file_name = dir_path + "/" + hash_hex[2:]

            if not is_file(dir_path):
                os.mkdir(dir_path)

            try:
                f = open(file_name, "wb")
                f.write(blob.wire)
                f.close()
            except Exception:
                return False

        return True

    def _get_blob(self, ptr: bytes) -> Optional[Blob]:
        """
        Creates and returns a Blob instance of the blob file that the
        given pointer is pointing to.
        """
        # get path of _blobs folder
        hex_hash = to_hex(ptr)
        split = self.file_name.split("/")
        # first two bytes of hash are the name of the subdirectory
        # ab23e5g... -> _blobs/ab/23e5g...
        file_name = "/".join(split[:-2]) + "/_blobs/" + hex_hash[:2]
        file_name += "/" + hex_hash[2:]

        try:
            f = open(file_name, "rb")
            content = f.read(120)
            f.close()
        except Exception:
            return None

        # create blob instance
        return Blob(content[:100], content[100:])

    def has_ended(self) -> bool:
        """
        Returns 'True' if this Feed instance was ended by a 'contdas' packet.
        """
        if len(self) < 1:
            return False
        return self.get_type(-1) == PacketType.contdas

    def get_parent(self) -> Optional[bytes]:
        """
        Returns the feed ID of this feed's parent feed.
        If this is not a child feed, 'None' is returned.
        """
        if self.anchor_seq != 0:
            return None

        if self.front_seq < 1:
            return None

        # optimization, only read file once
        wire = self.get_wire(1)
        # check type
        if slice_type(wire) != PacketType.ischild:
            return None
        # get parent FID
        return slice_fid(wire)

    def get_children(self) -> List[bytes]:
        """
        Returns a list of all child feed IDs contained within this feed.
        """
        children = []
        for i in range(self.anchor_seq + 1, self.front_seq + 1):
            # only read file once
            wire = self.get_wire(i)
            if wire[7:8] == PacketType.mkchild:
                children.append(wire[8:40])

        return children

    def get_children_index(self) -> List[Tuple[bytes, int]]:
        """
        Returns a list of all child feed IDs and their indices in the parent feed
        contained within this feed. Used for aligning feeds when printing them
        in the FeedManager.
        """
        children = []
        for i in range(self.anchor_seq + 1, self.front_seq + 1):
            wire = self.get_wire(i)
            if slice_type(wire) == PacketType.mkchild:
                children.append((slice_fid(wire), i))

        return children

    def get_contn(self) -> Optional[bytes]:
        """
        Returns the feed ID of this feed's continuation feed.
        If this feed has not ended, 'None' is returned.
        """
        if len(self) < 1:
            return None

        wire = self.get_wire(-1)

        if slice_type(wire) != PacketType.contdas:
            return None

        return slice_fid(wire)

    def get_prev(self) -> Optional[bytes]:
        """
        Returns the feed ID of this feed's predecessor feed.
        If this feed does not have a predecessor, 'None' is returned.
        """
        if self.anchor_seq != 0:
            return None

        wire = self.get_wire(1)

        if slice_type(wire) != PacketType.iscontn:
            return None

        return slice_fid(wire)

    def get_front(self) -> Tuple[int, bytes]:
        """
        Returns this feed's front sequence number and front message ID as a tuple.
        """
        assert self.front_mid is not None
        return (self.front_seq, self.front_mid)

    def get_next_dmx(self) -> Optional[bytes]:
        """
        Computes the dmx value of the next expected packet.
        """
        assert self.front_mid is not None, "no front message ID found"

        next_seq = (self.front_seq + 1).to_bytes(4, "big")
        next = Packet.prefix + self.fid + next_seq + self.front_mid
        return hashlib.sha256(next).digest()[:7]

    def waiting_for_blob(self) -> Optional[bytes]:
        """
        Checks whether this Feed instance is waiting for missing blobs.
        If it is not waiting None is returned.
        If it is waiting, the pointer to the missing blob is returned.
        """
        if len(self) < 1:
            return None

        # get wire format of last packet
        wire = self.get_wire(-1)

        if slice_type(wire) != PacketType.chain20:
            return None

        # front packet is blob, check if complete
        ptr = wire[36:56]  # 8:56 -> payload, last 20 bytes ptr
        if ptr == bytes(20):
            # self-contained blob
            return None

        while ptr != bytes(20):
            try:
                blob = self._get_blob(ptr)
                assert blob is not None
                ptr = blob.ptr
            except Exception:
                return ptr

        return None

    def verify_and_append_blob(self, blob_wire: bytes) -> bool:
        """
        Checks whether the given blob is missing in this feed.
        If the signature is correct, the blob is saved as a file and True is returned.
        """
        assert len(blob_wire) == 120, "blobs must be 120B"
        blob = Blob(blob_wire[:-20], blob_wire[-20:])

        # check if blob is missing
        if self.waiting_for_blob() != blob.signature:
            return False

        # append
        return self._write_blob([blob])

    def get_want(self) -> bytes:
        """
        Returns the current want request as bytes. This can be sent to other
        clients in order to receive a missing packet or blob.
        """
        want_dmx = hashlib.sha256(self.fid + b"want").digest()[:7]
        blob_ptr = self.waiting_for_blob()

        if blob_ptr is None:
            # packet missing
            next_seq = (self.front_seq + 1).to_bytes(4, "big")
            return want_dmx + self.fid + next_seq
        else:
            # blob missing
            next_seq = self.front_seq.to_bytes(4, "big")
            return want_dmx + self.fid + next_seq + blob_ptr

    def add_upd_file_name(
        self, file_name: Union[str, bytes], v_number: int = 0
    ) -> None:
        """
        Adds the given file name to this feed in the form of an updfile packet.
        """
        assert self.skey is not None, "need signing key to append pkt"
        assert self.front_mid is not None, "no front mid found"
        assert v_number >= 0, "version number can't be negative"

        pkt = create_upd_pkt(
            self.fid, self.front_seq + 1, self.front_mid, file_name, v_number, self.skey
        )
        self.append_pkt(pkt)

    def get_upd_file_name(self) -> Optional[str]:
        """
        Returns the file name that is stored in the first updfile packet
        that is appended to this feed. If no file name is found, None is
        returned.
        """
        for i in range(self.anchor_seq + 1, self.front_seq + 1):
            wire = self.get_wire(i)
            if slice_type(wire) == PacketType.updfile:
                file_name = slice_payload(wire)
                length, num_bytes = from_var_int(file_name)
                return file_name[num_bytes : length + 1].decode()
        return None

    def get_upd_version(self) -> Optional[int]:
        """
        Returns the version number that is stored in the upd packet.
        If no upd packet is appended, None is returned.
        """
        for i in range(self.anchor_seq + 1, self.front_seq + 1):
            wire = self.get_wire(i)
            if slice_type(wire) == PacketType.updfile:
                content = slice_payload(wire)
                length, num_bytes = from_var_int(content)
                offset = length + num_bytes
                return int.from_bytes(content[offset : offset + 4], "big")
        return None

    def get_current_version_num(self) -> Optional[int]:
        """
        Computes the version number of the newest update that is appended to this feed.
        Every appended blob is considered an update.
        """
        base_version = self.get_upd_version()
        if base_version is None:
            return None

        num_updates = self.count_chain20()
        return base_version + num_updates - 1  # base version is equal to the first blob

    def add_apply(self, fid: bytes, seq: Union[bytes, int]) -> None:
        """
        Adds a command containing the fid and the sequence number of an update
        that should be applied.
        """
        assert self.skey is not None, "need signing key to append packet"
        assert self.front_mid is not None, "no front mid found"

        pkt = create_apply_pkt(
            self.fid, self.front_seq + 1, self.front_mid, fid, seq, self.skey
        )
        self.append_pkt(pkt)

    def get_newest_apply(self, fid: bytes) -> int:
        """
        Searches for the newest apply packet for the given fid and returns
        its sequence number.
        If no apply is found, 0 is returned.
        """
        for i in range(self.front_seq, self.anchor_seq, -1):
            # go backwards, first -> newest
            wire = self.get_wire(i)
            if slice_type(wire) == PacketType.applyup:
                content = slice_payload(wire)
                if content[:32] == fid:
                    return int.from_bytes(content[32:36], "big")

        return 0

    def get_update_blob(self, v_num: int) -> Optional[bytes]:
        """
        Returns the blob with the given version number.
        The version number is different from the sequence number.
        If the version number is not found, None is returned.
        """
        min_version = self.get_upd_version()
        if min_version is None:
            return None

        max_version = self.get_current_version_num()
        if max_version is None:
            return None

        if v_num < min_version or v_num > max_version:
            return None

        return self.get_chain20(v_num - min_version)

    def get_dependency(self, v_num: int) -> Optional[int]:
        """
        Returns the version number of the dependency that the update with the
        given version number has. Only the headers of the blobs are read, making
        this faster and possible without having full blob available locally.
        """
        min_version = self.get_upd_version()
        if min_version is None:
            return None

        max_version = self.get_current_version_num()
        if max_version is None:
            return None

        if v_num < min_version or v_num > max_version:
            return None

        update_count = 0
        head_wire = None
        for i in range(self.anchor_seq + 1, self.front_seq + 1):
            wire = self.get_wire(i)
            if slice_type(wire) == PacketType.chain20:
                if update_count == v_num - min_version:
                    # found
                    head_wire = wire
                    break
                update_count += 1

        if head_wire is None:
            return None

        # extract dependency
        payload = slice_payload(head_wire)
        _, nbytes = from_var_int(payload)
        # cut off var int
        payload = payload[nbytes:]
        dependency = int.from_bytes(payload[:4], "big")

        return dependency

    # -------------------------------------------------------------------------------
    #   DEPRECATED
    # -------------------------------------------------------------------------------

    def get_blob_chain(self, pkt: Packet) -> bytes:
        """
        Retrieves the full data that a 'chain20' packet is pointing to.
        The content is validated. If the validation fails, 'None' is returned.
        """
        assert pkt.pkt_type == PacketType.chain20, "pkt type must be chain20"

        payload = b""
        ptr = pkt.payload[-20:]
        while ptr != bytes(20):
            blob = self._get_blob(ptr)
            assert blob is not None, "chaining of blobs failed"
            ptr = blob.ptr
            payload += blob.payload

        content_len, num_bytes = from_var_int(payload)
        return payload[num_bytes : num_bytes + content_len]

    def _verify_chain(self, head: Packet, blobs: List[Blob]) -> Optional[bytes]:
        """
        Verifies the authenticity of a given blob chain.
        If it is valid, the content is returned as bytes.
        """
        content_len, num_bytes = from_var_int(head.payload)
        ptr = head.payload[-20:]
        content = head.payload[num_bytes:-20]

        for blob in blobs:
            if ptr != blob.signature:
                return None
            content += blob.payload
            ptr = blob.ptr

        return content[:content_len]
