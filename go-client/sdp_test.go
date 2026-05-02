package hikws

import (
	"testing"
)

// Sample SDP from a typical Hikvision camera realplay response
const sampleSDP = `v=0
o=- 1234567890 1234567890 IN IP4 192.168.1.100
s=Hikvision Stream
c=IN IP4 192.168.1.100
t=0 0
m=video 0 RTP/AVP 96
a=rtpmap:96 H264/90000
a=fmtp:96 packetization-mode=1;profile-level-id=42001f;sprop-parameter-sets=Z0LAH9oC2A==,aM48gA==
a=control:track1
m=audio 0 RTP/AVP 8
a=rtpmap:8 PCMA/8000
`

func TestParseSDP_Basic(t *testing.T) {
	info := ParseSDP(sampleSDP)
	if info == nil {
		t.Fatal("ParseSDP returned nil")
	}

	// Origin
	if info.OriginUsername != "-" {
		t.Errorf("OriginUsername = %q, want %q", info.OriginUsername, "-")
	}
	if info.OriginAddress != "192.168.1.100" {
		t.Errorf("OriginAddress = %q, want %q", info.OriginAddress, "192.168.1.100")
	}

	// Session
	if info.SessionName != "Hikvision Stream" {
		t.Errorf("SessionName = %q, want %q", info.SessionName, "Hikvision Stream")
	}

	// Connection
	if info.ConnectionAddr != "192.168.1.100" {
		t.Errorf("ConnectionAddr = %q, want %q", info.ConnectionAddr, "192.168.1.100")
	}

	// Video
	if info.VideoCodec != "H264" {
		t.Errorf("VideoCodec = %q, want %q", info.VideoCodec, "H264")
	}
	if info.VideoClockRate != 90000 {
		t.Errorf("VideoClockRate = %d, want %d", info.VideoClockRate, 90000)
	}
	if info.VideoPayload != 96 {
		t.Errorf("VideoPayload = %d, want %d", info.VideoPayload, 96)
	}
	if info.VideoProfile != "42001f" {
		t.Errorf("VideoProfile = %q, want %q", info.VideoProfile, "42001f")
	}

	// Audio
	if info.AudioCodec != "PCMA" {
		t.Errorf("AudioCodec = %q, want %q", info.AudioCodec, "PCMA")
	}
	if info.AudioClockRate != 8000 {
		t.Errorf("AudioClockRate = %d, want %d", info.AudioClockRate, 8000)
	}
}

func TestParseSDP_SPSPPS(t *testing.T) {
	info := ParseSDP(sampleSDP)
	if info == nil {
		t.Fatal("ParseSDP returned nil")
	}

	if info.SPS == nil {
		t.Error("SPS is nil, expected decoded bytes")
	}
	if info.PPS == nil {
		t.Error("PPS is nil, expected decoded bytes")
	}

	// SPS NAL type should be 7 (SPS)
	if len(info.SPS) > 0 && (info.SPS[0]&0x1f) != 7 {
		t.Errorf("SPS NAL type = 0x%02x, want 0x07", info.SPS[0]&0x1f)
	}

	// PPS NAL type should be 8 (PPS)
	if len(info.PPS) > 0 && (info.PPS[0]&0x1f) != 8 {
		t.Errorf("PPS NAL type = 0x%02x, want 0x08", info.PPS[0]&0x1f)
	}
}

func TestParseSDP_BuildAnnexBSPSPPS(t *testing.T) {
	info := ParseSDP(sampleSDP)
	if info == nil {
		t.Fatal("ParseSDP returned nil")
	}

	result := info.BuildAnnexBSPSPPS()
	if result == nil {
		t.Fatal("BuildAnnexBSPSPPS returned nil")
	}

	// Should start with Annex B start code 00 00 00 01
	if len(result) < 4 || result[0] != 0x00 || result[1] != 0x00 || result[2] != 0x00 || result[3] != 0x01 {
		t.Errorf("Annex B result doesn't start with start code: %x", result[:4])
	}

	// Should contain two start codes (one for SPS, one for PPS)
	startCodes := 0
	for i := 0; i <= len(result)-4; i++ {
		if result[i] == 0x00 && result[i+1] == 0x00 && result[i+2] == 0x00 && result[i+3] == 0x01 {
			startCodes++
		}
	}
	if startCodes != 2 {
		t.Errorf("Found %d start codes, want 2", startCodes)
	}
}

func TestParseSDP_BuildMPEGPSPES(t *testing.T) {
	info := ParseSDP(sampleSDP)
	if info == nil {
		t.Fatal("ParseSDP returned nil")
	}

	result := info.BuildMPEGPSPES()
	if result == nil {
		t.Fatal("BuildMPEGPSPES returned nil")
	}

	// Should start with MPEG-PS pack start code 00 00 01 BA
	if len(result) < 4 || result[0] != 0x00 || result[1] != 0x00 || result[2] != 0x01 || result[3] != 0xba {
		t.Errorf("MPEG-PS PES doesn't start with pack start code: %x", result[:4])
	}

	// Should contain PES start code 00 00 01 E0 (video stream)
	foundPES := false
	for i := 0; i <= len(result)-4; i++ {
		if result[i] == 0x00 && result[i+1] == 0x00 && result[i+2] == 0x01 && result[i+3] == 0xe0 {
			foundPES = true
			break
		}
	}
	if !foundPES {
		t.Error("MPEG-PS PES doesn't contain video PES start code 00 00 01 E0")
	}
}

func TestParseSDP_Empty(t *testing.T) {
	info := ParseSDP("")
	if info != nil {
		t.Error("ParseSDP('') should return nil")
	}
}

func TestParseSDP_NoSpropParameterSets(t *testing.T) {
	sdp := `v=0
o=- 0 0 IN IP4 0.0.0.0
s=Test
m=video 0 RTP/AVP 96
a=rtpmap:96 H264/90000
`
	info := ParseSDP(sdp)
	if info == nil {
		t.Fatal("ParseSDP returned nil for SDP without sprop-parameter-sets")
	}
	if info.SPS != nil || info.PPS != nil {
		t.Error("SPS/PPS should be nil when sprop-parameter-sets is absent")
	}
	if info.BuildAnnexBSPSPPS() != nil {
		t.Error("BuildAnnexBSPSPPS should return nil when SPS/PPS absent")
	}
	if info.BuildMPEGPSPES() != nil {
		t.Error("BuildMPEGPSPES should return nil when SPS/PPS absent")
	}
}

func TestParseSDP_H265(t *testing.T) {
	sdp := `v=0
o=- 0 0 IN IP4 0.0.0.0
s=H265 Test
m=video 0 RTP/AVP 96
a=rtpmap:96 H265/90000
a=fmtp:96 sprop-vps=QAEMAf//AWAAAAMAsAAAAwAAAwB4AwA=;sprop-sps=QgEBAWAAAAMAsAAAAwAAAwB4AwA=;sprop-pps=RAHgdrA=
`
	info := ParseSDP(sdp)
	if info == nil {
		t.Fatal("ParseSDP returned nil")
	}
	if info.VideoCodec != "H265" {
		t.Errorf("VideoCodec = %q, want %q", info.VideoCodec, "H265")
	}
	// H.265 uses sprop-vps/sps/pps (not sprop-parameter-sets), so SPS/PPS should be nil
	// This is expected — full H.265 SDP parsing is a future enhancement
	if info.SPS != nil {
		t.Log("Note: H.265 sprop-sps parsing not yet supported (uses different parameter format)")
	}
}

func TestSDPInfo_String(t *testing.T) {
	info := ParseSDP(sampleSDP)
	if info == nil {
		t.Fatal("ParseSDP returned nil")
	}

	s := info.String()
	if s == "" {
		t.Error("String() returned empty string")
	}
	// Should contain key information
	if !contains(s, "H264") {
		t.Error("String() should contain 'H264'")
	}
	if !contains(s, "PCMA") {
		t.Error("String() should contain 'PCMA'")
	}
}

func contains(s, substr string) bool {
	for i := 0; i <= len(s)-len(substr); i++ {
		if s[i:i+len(substr)] == substr {
			return true
		}
	}
	return false
}
