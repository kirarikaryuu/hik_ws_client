package hikws

import (
	"encoding/base64"
	"fmt"
	"strconv"
	"strings"
)

// SDPInfo holds parsed information from an SDP (Session Description Protocol) string
// as returned by the Hikvision WebSocket realplay response.
//
// A typical SDP from a Hikvision camera looks like:
//
//	v=0
//	o=- 123456789 123456789 IN IP4 192.168.1.100
//	s=Hikvision Stream
//	c=IN IP4 192.168.1.100
//	t=0 0
//	m=video 0 RTP/AVP 96
//	a=rtpmap:96 H264/90000
//	a=fmtp:96 packetization-mode=1;profile-level-id=42001f;sprop-parameter-sets=Z0IAH5ZUCgL...,aM48gA==
//	a=control:track1
//	m=audio 0 RTP/AVP 8
//	a=rtpmap:8 PCMA/8000
type SDPInfo struct {
	// Origin
	OriginUsername string // o= <username>
	OriginSession  string // o= <sess-id>
	OriginAddress  string // o= <nettype> <addrtype> <unicast-address>

	// Session
	SessionName string // s= <session name>

	// Connection
	ConnectionAddr string // c= <connection-address>

	// Video media (m=video)
	VideoPort      int    // m=video <port>
	VideoPayload   int    // m=video ... <fmt> (payload type)
	VideoCodec     string // a=rtpmap:<payload> <encoding>/...  e.g. "H264"
	VideoClockRate int    // a=rtpmap:<payload> <encoding>/<clockrate>
	VideoProfile   string // a=fmtp:... profile-level-id=XXXXXX

	// SPS/PPS NAL units (decoded from sprop-parameter-sets)
	SPS []byte // Sequence Parameter Set
	PPS []byte // Picture Parameter Set

	// Audio media (m=audio)
	AudioPort      int
	AudioPayload   int
	AudioCodec     string // e.g. "PCMA"
	AudioClockRate int

	// Raw SDP for advanced inspection
	Raw string
}

// ParseSDP parses a raw SDP string and extracts structured information.
// Returns nil if the SDP is empty or cannot be meaningfully parsed.
func ParseSDP(sdp string) *SDPInfo {
	if sdp == "" {
		return nil
	}

	info := &SDPInfo{Raw: sdp}

	lines := strings.Split(sdp, "\n")
	var inVideo, inAudio bool

	for _, line := range lines {
		line = strings.TrimRight(line, "\r")
		if len(line) < 2 {
			continue
		}

		prefix := line[:2]
		value := strings.TrimSpace(line[2:])

		switch prefix {
		case "o=":
			parseOrigin(info, value)
		case "s=":
			info.SessionName = value
		case "c=":
			info.ConnectionAddr = parseConnectionAddr(value)
		case "m=":
			inVideo, inAudio = parseMediaLine(info, value)
		case "a=":
			if inVideo {
				parseVideoAttribute(info, value)
			} else if inAudio {
				parseAudioAttribute(info, value)
			}
		}
	}

	// Decode SPS/PPS if sprop-parameter-sets was found
	info.decodeSpropParameterSets()

	return info
}

// parseOrigin parses "o=<username> <sess-id> <sess-version> <nettype> <addrtype> <unicast-address>"
func parseOrigin(info *SDPInfo, value string) {
	parts := strings.Fields(value)
	if len(parts) >= 1 {
		info.OriginUsername = parts[0]
	}
	if len(parts) >= 2 {
		info.OriginSession = parts[1]
	}
	if len(parts) >= 6 {
		info.OriginAddress = parts[5]
	}
}

// parseConnectionAddr extracts the address from "c=IN IP4 <address>" or "c=IN IP6 <address>"
func parseConnectionAddr(value string) string {
	parts := strings.Fields(value)
	if len(parts) >= 3 {
		return parts[2]
	}
	return value
}

// parseMediaLine parses "m=<media> <port> <proto> <fmt>"
// Returns (isVideo, isAudio) booleans.
func parseMediaLine(info *SDPInfo, value string) (bool, bool) {
	parts := strings.Fields(value)
	if len(parts) < 4 {
		return false, false
	}

	media := parts[0]
	port, _ := strconv.Atoi(parts[1])
	// proto := parts[2]
	payload, _ := strconv.Atoi(parts[3])

	switch media {
	case "video":
		info.VideoPort = port
		info.VideoPayload = payload
		return true, false
	case "audio":
		info.AudioPort = port
		info.AudioPayload = payload
		return false, true
	default:
		return false, false
	}
}

// parseVideoAttribute processes attributes within the video media section.
func parseVideoAttribute(info *SDPInfo, value string) {
	// a=rtpmap:<payload> <encoding>/<clockrate>[/<encodingparams>]
	if strings.HasPrefix(value, "rtpmap:") {
		afterColon := value[7:]
		spaceIdx := strings.Index(afterColon, " ")
		if spaceIdx == -1 {
			return
		}
		// payloadType := afterColon[:spaceIdx]
		encoding := afterColon[spaceIdx+1:]

		parts := strings.SplitN(encoding, "/", 3)
		if len(parts) >= 1 {
			info.VideoCodec = parts[0]
		}
		if len(parts) >= 2 {
			info.VideoClockRate, _ = strconv.Atoi(parts[1])
		}
	}

	// a=fmtp:<payload> <params>
	if strings.HasPrefix(value, "fmtp:") {
		afterColon := value[5:]
		spaceIdx := strings.Index(afterColon, " ")
		if spaceIdx == -1 {
			return
		}
		params := afterColon[spaceIdx+1:]

		// Extract profile-level-id
		for _, param := range strings.Split(params, ";") {
			param = strings.TrimSpace(param)
			if strings.HasPrefix(param, "profile-level-id=") {
				info.VideoProfile = param[17:]
			}
			// sprop-parameter-sets is handled separately in decodeSpropParameterSets
		}
	}
}

// parseAudioAttribute processes attributes within the audio media section.
func parseAudioAttribute(info *SDPInfo, value string) {
	if strings.HasPrefix(value, "rtpmap:") {
		afterColon := value[7:]
		spaceIdx := strings.Index(afterColon, " ")
		if spaceIdx == -1 {
			return
		}
		encoding := afterColon[spaceIdx+1:]

		parts := strings.SplitN(encoding, "/", 3)
		if len(parts) >= 1 {
			info.AudioCodec = parts[0]
		}
		if len(parts) >= 2 {
			info.AudioClockRate, _ = strconv.Atoi(parts[1])
		}
	}
}

// decodeSpropParameterSets extracts and base64-decodes SPS/PPS from the
// a=fmtp line's sprop-parameter-sets parameter.
//
// Format: sprop-parameter-sets=<base64_sps>,<base64_pps>[,<base64_sps_ext>]
func (info *SDPInfo) decodeSpropParameterSets() {
	raw := info.Raw

	// Find sprop-parameter-sets in the raw SDP
	idx := strings.Index(raw, "sprop-parameter-sets=")
	if idx == -1 {
		return
	}

	start := idx + len("sprop-parameter-sets=")
	rest := raw[start:]

	// Value ends at next ; or newline
	end := len(rest)
	if i := strings.IndexAny(rest, ";\r\n"); i != -1 {
		end = i
	}
	paramSets := strings.TrimSpace(rest[:end])

	parts := strings.Split(paramSets, ",")
	if len(parts) < 2 {
		return
	}

	// Decode SPS (part 0)
	if sps, err := base64.StdEncoding.DecodeString(parts[0]); err == nil && len(sps) > 0 {
		info.SPS = sps
	}

	// Decode PPS (part 1)
	if pps, err := base64.StdEncoding.DecodeString(parts[1]); err == nil && len(pps) > 0 {
		info.PPS = pps
	}
}

// BuildAnnexBSPSPPS constructs SPS+PPS NAL units in Annex B format
// (with 00 00 00 01 start codes) from the decoded SPS/PPS in the SDP.
//
// This is useful for injecting SPS/PPS into raw H.264 bitstreams where
// FFmpeg reports "non-existing PPS 0 referenced" because the stream
// lacks parameter sets.
//
// Returns nil if SPS or PPS is not available.
func (info *SDPInfo) BuildAnnexBSPSPPS() []byte {
	if info.SPS == nil || info.PPS == nil {
		return nil
	}

	startCode := []byte{0x00, 0x00, 0x00, 0x01}
	var result []byte
	result = append(result, startCode...)
	result = append(result, info.SPS...)
	result = append(result, startCode...)
	result = append(result, info.PPS...)
	return result
}

// BuildMPEGPSPES constructs an MPEG-PS PES packet wrapping the SPS+PPS NAL units.
// This is needed for MPEG-PS encapsulated streams where SPS/PPS must be injected
// as a valid PES packet rather than raw Annex B data.
//
// Returns nil if SPS or PPS is not available.
func (info *SDPInfo) BuildMPEGPSPES() []byte {
	nalUnits := info.BuildAnnexBSPSPPS()
	if nalUnits == nil {
		return nil
	}

	// Pack Header (14 bytes)
	packHeader := []byte{
		0x00, 0x00, 0x01, 0xba, // pack start code
		0x44, 0x00, 0x04, 0x00, // SCR = 0, marker bits
		0x04, 0x01, // SCR extension + mux rate
		0x00, 0x89, 0xc3, // mux_rate + pack stuffing
	}

	// PES Header
	// 00 00 01 e0 = video stream PES start code
	pesPacketLen := 2 + len(nalUnits)
	if pesPacketLen > 0xFFFF {
		pesPacketLen = 0xFFFF
	}

	pes := make([]byte, 0, 14+6+len(nalUnits))
	pes = append(pes, packHeader...)
	pes = append(pes, 0x00, 0x00, 0x01, 0xe0)                  // PES start code (video)
	pes = append(pes, byte(pesPacketLen>>8), byte(pesPacketLen&0xFF)) // PES packet length
	pes = append(pes, 0x00) // PTS_DTS_flags = 00
	pes = append(pes, 0x00) // PES_header_data_length = 0
	pes = append(pes, nalUnits...)

	return pes
}

// String returns a human-readable summary of the SDP info.
func (info *SDPInfo) String() string {
	if info == nil {
		return "<nil>"
	}

	var b strings.Builder
	fmt.Fprintf(&b, "SDP Session: %s\n", info.SessionName)
	fmt.Fprintf(&b, "  Origin: %s@%s (session %s)\n", info.OriginUsername, info.OriginAddress, info.OriginSession)
	fmt.Fprintf(&b, "  Connection: %s\n", info.ConnectionAddr)

	if info.VideoCodec != "" {
		fmt.Fprintf(&b, "  Video: %s/%d Hz (payload %d, port %d)\n",
			info.VideoCodec, info.VideoClockRate, info.VideoPayload, info.VideoPort)
		if info.VideoProfile != "" {
			fmt.Fprintf(&b, "    Profile-Level-ID: %s\n", info.VideoProfile)
		}
		if info.SPS != nil {
			fmt.Fprintf(&b, "    SPS: %d bytes (NAL type=0x%02x)\n", len(info.SPS), info.SPS[0]&0x1f)
		}
		if info.PPS != nil {
			fmt.Fprintf(&b, "    PPS: %d bytes (NAL type=0x%02x)\n", len(info.PPS), info.PPS[0]&0x1f)
		}
	}

	if info.AudioCodec != "" {
		fmt.Fprintf(&b, "  Audio: %s/%d Hz (payload %d, port %d)\n",
			info.AudioCodec, info.AudioClockRate, info.AudioPayload, info.AudioPort)
	}

	return b.String()
}
