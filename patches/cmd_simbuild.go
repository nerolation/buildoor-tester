package cmd

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"github.com/ethereum/go-ethereum/common"
	"github.com/ethereum/go-ethereum/core/types"
	"github.com/spf13/cobra"

	"github.com/ethpandaops/buildoor/pkg/builder"
	"github.com/ethpandaops/buildoor/pkg/rpc/engine"
)

// simbuildCmd starts a minimal HTTP server that exposes the buildoor build
// pipeline (RequestPayloadBuild + GetPayloadRaw + ModifyPayloadExtraData)
// without the rest of the builder stack. Designed for replay harnesses like
// eest-replay that drive blocks without a real consensus layer.
var simbuildCmd = &cobra.Command{
	Use:   "simbuild",
	Short: "Run a CL-less build endpoint for replay harnesses",
	Long: `simbuild starts a small HTTP server that calls into buildoor's payload
build pipeline on demand. It accepts payload attributes and a parent block
hash, requests a build from the configured Engine API, and returns the
resulting ExecutionPayload (with buildoor's extraData modifier applied).

No beacon-node, BLS signer, or lifecycle is required — only the engine
endpoint and a JWT secret.`,
	RunE: runSimbuild,
}

func init() {
	simbuildCmd.Flags().String("listen-addr", ":18551", "Address for the simbuild HTTP server")
	simbuildCmd.Flags().Uint64("simbuild-build-wait-ms", 500, "Time to let the EL build before getPayload (ms)")
	if err := v.BindPFlag("listen-addr", simbuildCmd.Flags().Lookup("listen-addr")); err != nil {
		panic(err)
	}
	if err := v.BindPFlag("simbuild-build-wait-ms", simbuildCmd.Flags().Lookup("simbuild-build-wait-ms")); err != nil {
		panic(err)
	}
	rootCmd.AddCommand(simbuildCmd)
}

func runSimbuild(cmd *cobra.Command, args []string) error {
	if cfg.ELEngineAPI == "" {
		return fmt.Errorf("--el-engine-api is required")
	}
	if cfg.ELJWTSecret == "" {
		return fmt.Errorf("--el-jwt-secret is required")
	}

	ctx, cancel := context.WithCancel(cmd.Context())
	defer cancel()

	logger.Info("simbuild: connecting to execution layer engine API")
	engineClient, err := engine.NewClient(ctx, cfg.ELEngineAPI, cfg.ELJWTSecret, logger)
	if err != nil {
		return fmt.Errorf("connect engine API: %w", err)
	}
	defer engineClient.Close()

	listen := v.GetString("listen-addr")
	wait := time.Duration(v.GetUint64("simbuild-build-wait-ms")) * time.Millisecond
	srv := &simbuildServer{
		engine:    engineClient,
		buildWait: wait,
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/build", srv.handleBuild)
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })

	httpSrv := &http.Server{
		Addr:              listen,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}

	logger.WithField("addr", listen).Info("simbuild: HTTP server listening")
	return httpSrv.ListenAndServe()
}

// simbuildServer holds the dependencies for the /build handler.
type simbuildServer struct {
	engine    *engine.Client
	buildWait time.Duration
}

// buildRequest is the JSON body accepted by POST /build. All fields mirror the
// shape the EEST FixtureEngineNewPayload would produce.
type buildRequest struct {
	ParentHash            string            `json:"parent_hash"`
	Timestamp             string            `json:"timestamp"`
	PrevRandao            string            `json:"prev_randao"`
	SuggestedFeeRecipient string            `json:"suggested_fee_recipient"`
	ParentBeaconBlockRoot string            `json:"parent_beacon_block_root"`
	SlotNumber            string            `json:"slot_number"`
	TargetGasLimit        string            `json:"target_gas_limit"`
	Withdrawals           []buildWithdrawal `json:"withdrawals"`
}

type buildWithdrawal struct {
	Index          string `json:"index"`
	ValidatorIndex string `json:"validator_index"`
	Address        string `json:"address"`
	Amount         string `json:"amount"`
}

// buildResponse is the JSON body returned by POST /build. We surface only the
// payload JSON exactly as the engine API returned it (after buildoor's
// extraData modifier), so the caller can diff it against an EEST fixture.
type buildResponse struct {
	ExecutionPayload  json.RawMessage `json:"execution_payload"`
	BlockHash         string          `json:"block_hash"`
	BlobsBundle       any             `json:"blobs_bundle,omitempty"`
	ExecutionRequests []string        `json:"execution_requests,omitempty"`
	BlockValue        string          `json:"block_value,omitempty"`
}

func (s *simbuildServer) handleBuild(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	defer r.Body.Close()

	var req buildRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, fmt.Sprintf("decode body: %v", err), http.StatusBadRequest)
		return
	}

	attrs, err := req.toAttributes()
	if err != nil {
		http.Error(w, fmt.Sprintf("invalid attrs: %v", err), http.StatusBadRequest)
		return
	}

	parent := common.HexToHash(req.ParentHash)
	// safe = finalized = parent: there's no fork to choose, the replay harness
	// drives blocks deterministically from genesis along a single chain.
	payloadID, err := s.engine.RequestPayloadBuild(r.Context(), parent, parent, parent, attrs)
	if err != nil {
		http.Error(w, fmt.Sprintf("RequestPayloadBuild: %v", err), http.StatusInternalServerError)
		return
	}

	time.Sleep(s.buildWait)

	result, err := s.engine.GetPayloadRaw(r.Context(), payloadID)
	if err != nil {
		http.Error(w, fmt.Sprintf("GetPayloadRaw: %v", err), http.StatusInternalServerError)
		return
	}

	var beacon common.Hash
	if attrs.ParentBeaconBlockRoot != nil {
		beacon = *attrs.ParentBeaconBlockRoot
	}
	modified, newHash, err := builder.ModifyPayloadExtraData(
		result.ExecutionPayloadJSON,
		[]byte("buildoor/"),
		beacon,
		result.ExecutionRequests,
	)
	if err != nil {
		http.Error(w, fmt.Sprintf("ModifyPayloadExtraData: %v", err), http.StatusInternalServerError)
		return
	}

	resp := buildResponse{
		ExecutionPayload: modified,
		BlockHash:        newHash.Hex(),
	}
	if result.BlockValue != nil {
		resp.BlockValue = "0x" + result.BlockValue.Text(16)
	}
	if result.BlobsBundle != nil {
		resp.BlobsBundle = result.BlobsBundle
	}
	if len(result.ExecutionRequests) > 0 {
		hexReqs := make([]string, len(result.ExecutionRequests))
		for i, b := range result.ExecutionRequests {
			hexReqs[i] = "0x" + common.Bytes2Hex(b)
		}
		resp.ExecutionRequests = hexReqs
	}

	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(resp); err != nil {
		logger.WithError(err).Warn("simbuild: failed to encode response")
	}
}

func (r *buildRequest) toAttributes() (*engine.PayloadAttributes, error) {
	ts, err := parseHexU64(r.Timestamp)
	if err != nil {
		return nil, fmt.Errorf("timestamp: %w", err)
	}
	slot, err := parseHexU64(r.SlotNumber)
	if err != nil {
		return nil, fmt.Errorf("slot_number: %w", err)
	}
	target, err := parseHexU64(r.TargetGasLimit)
	if err != nil {
		return nil, fmt.Errorf("target_gas_limit: %w", err)
	}

	withdrawals := make([]*types.Withdrawal, 0, len(r.Withdrawals))
	for i, w := range r.Withdrawals {
		idx, err := parseHexU64(w.Index)
		if err != nil {
			return nil, fmt.Errorf("withdrawals[%d].index: %w", i, err)
		}
		val, err := parseHexU64(w.ValidatorIndex)
		if err != nil {
			return nil, fmt.Errorf("withdrawals[%d].validator_index: %w", i, err)
		}
		amt, err := parseHexU64(w.Amount)
		if err != nil {
			return nil, fmt.Errorf("withdrawals[%d].amount: %w", i, err)
		}
		withdrawals = append(withdrawals, &types.Withdrawal{
			Index:     idx,
			Validator: val,
			Address:   common.HexToAddress(w.Address),
			Amount:    amt,
		})
	}

	attrs := &engine.PayloadAttributes{
		Timestamp:             ts,
		PrevRandao:            common.HexToHash(r.PrevRandao),
		SuggestedFeeRecipient: common.HexToAddress(r.SuggestedFeeRecipient),
		Withdrawals:           withdrawals,
		SlotNumber:            slot,
		TargetGasLimit:        target,
	}
	if r.ParentBeaconBlockRoot != "" {
		h := common.HexToHash(r.ParentBeaconBlockRoot)
		attrs.ParentBeaconBlockRoot = &h
	}
	return attrs, nil
}

func parseHexU64(s string) (uint64, error) {
	if s == "" {
		return 0, nil
	}
	if len(s) > 2 && (s[:2] == "0x" || s[:2] == "0X") {
		s = s[2:]
	}
	if s == "" {
		return 0, nil
	}
	return strconv.ParseUint(s, 16, 64)
}
