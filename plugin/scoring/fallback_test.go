package scoring

import (
	"testing"

	"kaptain/snapshot"
)

// The fallback ranks on free millicores, not on ratios: the Pi, empty, must lose to the 8-core
// worker while that one has more cores left, and win once it has fewer.
func TestFallbackMostFreeCPU(t *testing.T) {
	s := &snapshot.Snapshot{
		Pod: snapshot.Pod{RequestedMilliCPU: 500, RequestedMemoryBytes: 1 << 30},
		Nodes: []snapshot.Node{
			{Name: "pi", AllocatableMilliCPU: 4000, AllocatableMemoryBytes: 8 << 30},
			{Name: "worker-1", AllocatableMilliCPU: 8000, AllocatableMemoryBytes: 8 << 30},
			{Name: "worker-2", AllocatableMilliCPU: 1000, AllocatableMemoryBytes: 2 << 30},
		},
	}
	for _, c := range []struct {
		requested int64
		want      string
	}{
		{3000, "worker-1"}, // 4.5 cores free against the Pi's 3.5
		{5000, "pi"},       // 2.5 against 3.5
		{4000, "worker-1"}, // 3.5 each: the larger allocatable
	} {
		s.Nodes[1].RequestedMilliCPU = c.requested
		if got := fallbackNode(s); got != c.want {
			t.Errorf("worker-1 requesting %dm: got %s, want %s", c.requested, got, c.want)
		}
	}
}

func TestFreeMilliCPUOverRequested(t *testing.T) {
	n := snapshot.Node{AllocatableMilliCPU: 1000, RequestedMilliCPU: 900}
	if got := freeMilliCPU(n, snapshot.Pod{RequestedMilliCPU: 500}); got != -400 {
		t.Errorf("got %d, want -400", got)
	}
}
