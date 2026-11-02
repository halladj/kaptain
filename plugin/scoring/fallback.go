package scoring

import (
	"sort"

	"kaptain/snapshot"
)

// FallbackName runs when something else already failed, so it uses no telemetry of its own.
// A fallback is a decision of the method under test: logged, counted, kept in the results.
const FallbackName = "most-free-cpu-requests"

// fallbackIncomplete reports the candidates whose allocatable or requested resources are
// missing. The fallback still has to answer -- it runs when something else already failed
// -- but a ranking built partly on zeros must say so rather than pass for a measurement.
func fallbackIncomplete(s *snapshot.Snapshot) []string {
	var blind []string
	for _, n := range s.Nodes {
		for _, feature := range n.Missing {
			if feature == snapshot.FeatureAllocatable || feature == snapshot.FeatureRequested {
				blind = append(blind, n.Name)
				break
			}
		}
	}
	return blind
}

func fallbackNode(s *snapshot.Snapshot) string {
	nodes := append([]snapshot.Node(nil), s.Nodes...)
	sort.Slice(nodes, func(i, j int) bool {
		left, right := nodes[i], nodes[j]
		if free := freeMilliCPU(left, s.Pod); free != freeMilliCPU(right, s.Pod) {
			return free > freeMilliCPU(right, s.Pod)
		}
		if left.AllocatableMilliCPU != right.AllocatableMilliCPU {
			return left.AllocatableMilliCPU > right.AllocatableMilliCPU
		}
		return left.Name < right.Name
	})
	if len(nodes) == 0 {
		return ""
	}
	return nodes[0].Name
}

func fallbackScores(s *snapshot.Snapshot) map[string]float64 {
	chosen := fallbackNode(s)
	scores := make(map[string]float64, len(s.Nodes))
	for _, n := range s.Nodes {
		scores[n.Name] = 0
	}
	if chosen != "" {
		scores[chosen] = 1
	}
	return scores
}

// freeMilliCPU is the CPU left unrequested once the incoming Pod is added, in millicores,
// not as a ratio. The ratios of CPU and memory, least-allocated's rule until now, sent most
// pods to the slow Pi, whose memory is as large as the biggest worker's. Absolute cores favour
// the nodes that can take the work. Negative when the node is over-requested: ranked last.
func freeMilliCPU(n snapshot.Node, pod snapshot.Pod) int64 {
	return n.AllocatableMilliCPU - n.RequestedMilliCPU - pod.RequestedMilliCPU
}
