# Use gRPC for the worker protocol

The Core and Workers use a versioned protobuf/gRPC interface over authenticated loopback connections. Framed stdio would reduce port management and HTTP would be simpler to inspect, but gRPC provides typed cross-language contracts, efficient binary audio streaming, deadlines, and cancellation at a seam expected to support several independently versioned adapters. The project accepts protobuf generation and packaging complexity in exchange.
