var ImageHandler = require("./imageHandler.js");
var ROT = require("./rot6.js");

var key2pt_cache = {};
function key2pt(key) {
    if (key in key2pt_cache) return key2pt_cache[key];
    var p = key.split(',').map(function (c) { return parseInt(c) });
    var pt = {x: p[0], y: p[1], key: key};
    key2pt_cache[key] = pt;
    return pt;
}

function xy2key(x, y) {
    return x + "," + y;
}

function xy2pt(x, y) {
    return {x: x, y: y, key: x + "," + y};
}

function pt2key(pt) {
    return pt.x + "," + pt.y;
}

function generateElevationWalls(data, elevation) {
    var t1 = Date.now();
    var walls = {};
    for (var key in data) {
        var pt = data[key];
        if (pt.z > elevation) {
            adjLoop:
            for (var i = -1; i <= 1; i++) {
                for (var j = -1; j <= 1; j++) {
                    if (0 !== i || 0 !== j) {
                        var k = (pt.x + i) + "," + (pt.y + j);
                        if (data[k] && data[k].z <= elevation) {
                            walls[pt.key] = pt;
                            break adjLoop;
                        }
                    }
                }
            }
        }
    }
    console.log('generateElevationWalls', Date.now() - t1 + 'ms');
    return walls;
}

function setElevationWalls(obj, data, elevation) {
    for (var i = 0; i < data[elevation].length; i++) {
        var el = data[elevation][i];
        obj[el[1] + "," + el[2]] = el;
    }
}

function setWalls(obj, data, id, r) {
    id = id || 'wall';
    r = r || (Math.SQRT2 / 2);
    for (var i in data) {
        obj[i] = [id, data[i].x, data[i].y, r];
    }
}

function setTreeWalls(obj, elevation, tree, tree_elevations, tree_state, tree_blocks) {
    for (var i in tree) {
        if (elevation < tree_elevations[i]) {
            if (tree_state[i]) {
                //obj[i] = ['tree', tree[i].x, tree[i].y, Math.SQRT2];
                tree_blocks[i].forEach(function (pt) {
                    var k = pt.x + "," + pt.y;
                    obj[k] = (obj[k] || []).concat([['tree', tree[i].x, tree[i].y, tree[i].shadowRadius]]);
                });
            }
        }
    }
}

function addObstacle(obj, key, obstacle) {
    if (obstacle) {
        obj[key] = (obj[key] || []).concat([obstacle]);
    } else {
        obj[key] = obj[key] || key2pt(key);
    }
}

function padBlockMap(data, radius) {
    if (!radius) return data;
    var out = {};
    for (var key in data) {
        var pt = data[key];
        for (var dx = -radius; dx <= radius; dx++) {
            for (var dy = -radius; dy <= radius; dy++) {
                if (dx * dx + dy * dy > radius * radius) continue;
                var paddedKey = (pt.x + dx) + "," + (pt.y + dy);
                out[paddedKey] = xy2pt(pt.x + dx, pt.y + dy);
            }
        }
    }
    return out;
}

function padObstacleMap(data, radius) {
    if (!radius) return data;
    var out = {};
    for (var key in data) {
        var obstacles = data[key];
        var pt = key2pt(key);
        for (var i = 0; i < obstacles.length; i++) {
            var obstacle = obstacles[i];
            for (var dx = -radius; dx <= radius; dx++) {
                for (var dy = -radius; dy <= radius; dy++) {
                    if (dx * dx + dy * dy > radius * radius) continue;
                    addObstacle(out, (pt.x + dx) + "," + (pt.y + dy), obstacle);
                }
            }
        }
    }
    return out;
}

function blockMapToObstacleMap(data, id, obstacleRadius) {
    var out = {};
    for (var key in data) {
        var pt = data[key];
        addObstacle(out, key, [id, pt.x, pt.y, obstacleRadius]);
    }
    return out;
}

function mergeObstacleMaps() {
    var out = {};
    for (var i = 0; i < arguments.length; i++) {
        var data = arguments[i] || {};
        for (var key in data) {
            var obstacles = data[key];
            out[key] = (out[key] || []).concat(obstacles);
        }
    }
    return out;
}

function clusterTreeObstacleMap(data, minCells, radius, treeShadowRadius) {
    if (!radius || !minCells) return data;
    var visited = {};
    var out = {};
    for (var key in data) {
        out[key] = (out[key] || []).concat(data[key]);
    }
    function neighbors(pt) {
        var result = [];
        for (var dx = -1; dx <= 1; dx++) {
            for (var dy = -1; dy <= 1; dy++) {
                if (dx === 0 && dy === 0) continue;
                result.push((pt.x + dx) + "," + (pt.y + dy));
            }
        }
        return result;
    }
    for (var startKey in data) {
        if (visited[startKey]) continue;
        var stack = [startKey];
        var component = [];
        visited[startKey] = true;
        while (stack.length) {
            var key = stack.pop();
            component.push(key);
            var pt = key2pt(key);
            var ns = neighbors(pt);
            for (var i = 0; i < ns.length; i++) {
                var nKey = ns[i];
                if (!data[nKey] || visited[nKey]) continue;
                visited[nKey] = true;
                stack.push(nKey);
            }
        }
        if (component.length < minCells) continue;
        for (var c = 0; c < component.length; c++) {
            var cpt = key2pt(component[c]);
            for (var px = -radius; px <= radius; px++) {
                for (var py = -radius; py <= radius; py++) {
                    if (px * px + py * py > radius * radius) continue;
                    var gx = cpt.x + px;
                    var gy = cpt.y + py;
                    var paddedKey = gx + "," + gy;
                    out[paddedKey] = (out[paddedKey] || []).concat([['tree', gx, gy, treeShadowRadius]]);
                }
            }
        }
    }
    return out;
}

function parseImage(imageHandler, offset, width, height, pixelHandler) {
    var grid = {};
    imageHandler.scan(offset, width, height, pixelHandler, grid);
    return grid;
}

function VisionSimulation(worlddata, opts) {
    var self = this;
    
    this.opts = opts || {};
    this.cellSize = this.opts.cellSize || 64;
    this.radius = this.opts.radius || parseInt(1600 / this.cellSize);
    this.treeBlockCells = this.opts.treeBlockCells || Math.max(2, Math.round(128 / this.cellSize));
    this.treeShadowRadiusCells = this.opts.treeShadowRadiusCells || Math.SQRT2;
    this.treeClusterMinCells = this.opts.treeClusterMinCells || 18;
    this.treeClusterPaddingCells = this.opts.treeClusterPaddingCells || 0;
    this.blockerPaddingCells = this.opts.blockerPaddingCells || 0;
    this.externalTreeRaycast = !!this.opts.externalTreeRaycast;
    this.externalTreeShape = this.opts.externalTreeShape || "circle";
    this.externalTreeBodyVisible = this.opts.externalTreeBodyVisible !== false;
    this.worldMinX = worlddata.worldMinX;
    this.worldMinY = worlddata.worldMinY;
    this.worldMaxX = worlddata.worldMaxX;
    this.worldMaxY = worlddata.worldMaxY;
    this.worldWidth = this.worldMaxX - this.worldMinX;
    this.worldHeight = this.worldMaxY - this.worldMinY;
    this.gridWidth = this.worldWidth / this.cellSize + 1;
    this.gridHeight = this.worldHeight / this.cellSize + 1;
    this.ready = false;

    this.lightPassesCallback = function (x, y) {
        var key = x + ',' + y;
        if (key === self.lightOriginKey) return true;
        return !(key in self.activeElevationWalls) && !(key in self.activeFowBlockers) && !(key in self.activeTreeWalls && self.activeTreeWalls[key].length > 0) ;
    }
    
    this.fov = new ROT.FOV.PreciseShadowcasting(this.lightPassesCallback, {topology:8});
}
VisionSimulation.prototype.initialize = function (mapDataImagePath, onReady) {
    var self = this;
    this.ready = false;
    this.grid = [];
    this.gridnav = null;
    this.ent_fow_blocker_node = null;
    this.tools_no_wards = null;
    this.elevationValues = [];
    this.elevationGrid = null;
    this.elevationWalls = {};
    this.treeWalls = {};
    this.clusteredTreeWalls = {};
    this.paddedElevationWalls = {};
    this.elevationWallObstacles = {};
    this.paddedTreeWalls = {};
    this.paddedFowBlockerNode = null;
    this.fowBlockerObstacles = null;
    this.activeElevationWalls = {};
    this.activeTreeWalls = {};
    this.activeFowBlockers = {};
    this.lightOriginKey = null;
    this.tree = {}; // center key to point map
    this.tree_blocks = {}; // center to corners map
    this.tree_relations = {}; // corner to center map
    this.tree_elevations = {};
    this.tree_state = {};
    this.externalTreeCircles = [];
    this.walls = {};
    this.lights = {};
    this.area = 0;
    if (this.imageHandler) this.imageHandler.disable();
    this.imageHandler = new ImageHandler(mapDataImagePath);
    var t1 = Date.now();
    this.imageHandler.load(function (err) {
        if (!err) {
            var t2 = Date.now();
            console.log('image load', t2 - t1 + 'ms');
            self.gridnav = parseImage(self.imageHandler, self.gridWidth * 2, self.gridWidth, self.gridHeight, self.blackPixelHandler.bind(self));
            self.ent_fow_blocker_node = parseImage(self.imageHandler, self.gridWidth * 3, self.gridWidth, self.gridHeight, self.blackPixelHandler.bind(self));
            self.tools_no_wards = parseImage(self.imageHandler, self.gridWidth * 4, self.gridWidth, self.gridHeight, self.blackPixelHandler.bind(self));
            parseImage(self.imageHandler, self.gridWidth, self.gridWidth, self.gridHeight, self.treeElevationPixelHandler.bind(self));
            self.elevationGrid = parseImage(self.imageHandler, 0, self.gridWidth, self.gridHeight, self.elevationPixelHandler.bind(self));
            var t3 = Date.now();
            console.log('image process', t3 - t2 + 'ms');
            self.elevationValues.forEach(function (elevation) {
                //self.elevationWalls[elevation] = generateElevationWalls(self.elevationGrid, elevation);
                self.treeWalls[elevation] = {};
                setTreeWalls(self.treeWalls[elevation], elevation, self.tree, self.tree_elevations, self.tree_state, self.tree_blocks)
            });
            var t4 = Date.now();
            console.log('walls generation', t4 - t3 + 'ms');
            for (var i = 0; i < self.gridWidth; i++) {
                self.grid[i] = [];
                for (var j = 0; j < self.gridHeight; j++) {
                    var pt = xy2pt(i, j);
                    key2pt_cache[pt.key] = pt;
                    self.grid[i].push(pt);
                }
            }
            var t5 = Date.now();
            console.log('cache prime', t5 - t4 + 'ms');
            self.ready = true;
        }
        onReady(err);
    });
}

VisionSimulation.prototype.blackPixelHandler = function (x, y, p, grid) {
    var pt = this.ImageXYtoGridXY(x, y);
    if (p[0] === 0) {
        grid[pt.x + "," + pt.y] = pt;
    }
}
VisionSimulation.prototype.elevationPixelHandler = function (x, y, p, grid) {
    var pt = this.ImageXYtoGridXY(x, y);
    pt.z = p[0];
    grid[pt.x + "," + pt.y] = pt;
    if (this.elevationValues.indexOf(p[0]) == -1) {
        this.elevationValues.push(p[0]);
    }
}
VisionSimulation.prototype.treeElevationPixelHandler = function (x, y, p, grid) {
    var self = this;
    var pt = this.ImageXYtoGridXY(x, y);
    if (p[1] == 0 && p[2] == 0) {
        // Trees cover roughly 128 world units. At the default 64-unit grid this is 2x2 cells;
        // at higher precision, expand the block square to keep the world-space size stable.
        var treeOrigin = xy2pt(pt.x - 0.5, pt.y - 0.5);
        treeOrigin.shadowRadius = this.treeShadowRadiusCells;
        var treeElevation = p[0] + 40;
        var kC = treeOrigin.key;
        this.tree[kC] = treeOrigin;
        this.tree_elevations[kC] = treeElevation;
        this.tree_blocks[kC] = [];
        this.tree_state[kC] = true;
        var blockCells = this.treeBlockCells;
        var startX = Math.floor(treeOrigin.x - (blockCells - 2) / 2);
        var startY = Math.floor(treeOrigin.y - (blockCells - 2) / 2);
        for (var i = 0; i < blockCells; i++) {
            for (var j = 0; j < blockCells; j++) {
                var treeCorner = xy2pt(startX + i, startY + j);
                self.tree_relations[treeCorner.key] = (self.tree_relations[treeCorner.key] || []).concat(treeOrigin);
                self.tree_blocks[kC].push(treeCorner);
            }
        }
    }
}
VisionSimulation.prototype.setExternalTrees = function (trees) {
    var self = this;
    this.tree = {};
    this.tree_blocks = {};
    this.tree_relations = {};
    this.tree_elevations = {};
    this.tree_state = {};
    this.treeWalls = {};
    this.clusteredTreeWalls = {};
    this.paddedTreeWalls = {};
    this.externalTreeCircles = [];

    trees.forEach(function (tree, index) {
        var center = self.WorldXYtoGridXY(Number(tree.x), Number(tree.y), true);
        var radiusCells = Math.max(0.01, Number(tree.radiusWorld) / self.cellSize);
        var centerKey = "external:" + index;
        center.key = centerKey;
        center.shadowRadius = radiusCells;

        var sample = self.WorldXYtoGridXY(Number(tree.x), Number(tree.y));
        var elevationPoint = self.elevationGrid[sample.key];
        if (!elevationPoint) return;

        self.tree[centerKey] = center;
        self.tree_elevations[centerKey] = elevationPoint.z + 40;
        self.tree_blocks[centerKey] = [];
        self.tree_state[centerKey] = true;
        self.externalTreeCircles.push({
            key: centerKey,
            x: center.x,
            y: center.y,
            radius: radiusCells,
            elevation: elevationPoint.z + 40
        });

        var blockCells = self.treeBlockCells;
        var startX = Math.floor(center.x - (blockCells - 2) / 2);
        var startY = Math.floor(center.y - (blockCells - 2) / 2);
        for (var gx = startX; gx < startX + blockCells; gx++) {
            for (var gy = startY; gy < startY + blockCells; gy++) {
                var block = xy2pt(gx, gy);
                self.tree_blocks[centerKey].push(block);
            }
        }
        self.tree_blocks[centerKey].forEach(function (block) {
            var padding = self.blockerPaddingCells;
            for (var px = -padding; px <= padding; px++) {
                for (var py = -padding; py <= padding; py++) {
                    if (px * px + py * py > padding * padding) continue;
                    var relationKey = (block.x + px) + "," + (block.y + py);
                    self.tree_relations[relationKey] = (self.tree_relations[relationKey] || []).concat(center);
                }
            }
        });
    });

    this.elevationValues.forEach(function (elevation) {
        self.treeWalls[elevation] = {};
        if (!self.externalTreeRaycast) {
            setTreeWalls(self.treeWalls[elevation], elevation, self.tree, self.tree_elevations, self.tree_state, self.tree_blocks);
        }
    });
}
VisionSimulation.prototype.applyExternalTreeRaycast = function (originX, originY, radius) {
    if (!this.externalTreeRaycast || !this.externalTreeCircles.length) return;

    var self = this;
    var activeTrees = this.externalTreeCircles.filter(function (tree) {
        if (!self.tree_state[tree.key] || self.elevation >= tree.elevation) return false;
        var dx = tree.x - originX;
        var dy = tree.y - originY;
        var limit = radius + tree.radius;
        return dx * dx + dy * dy <= limit * limit;
    });

    // A grid cell is visible only when its center has line of sight. Treating
    // any one of several sub-cell samples as visible creates thin leaks that
    // incorrectly pass through adjacent tree trunks.
    var sampleOffsets = [0];
    function segmentIntersectsSquare(targetX, targetY, tree) {
        var vx = targetX - originX;
        var vy = targetY - originY;
        var half = tree.radius;
        var tMin = 0;
        var tMax = 1;
        var axes = [
            [originX, vx, tree.x - half, tree.x + half],
            [originY, vy, tree.y - half, tree.y + half]
        ];
        for (var axis = 0; axis < axes.length; axis++) {
            var origin = axes[axis][0];
            var delta = axes[axis][1];
            var min = axes[axis][2];
            var max = axes[axis][3];
            if (Math.abs(delta) < 1e-9) {
                if (origin < min || origin > max) return false;
                continue;
            }
            var t1 = (min - origin) / delta;
            var t2 = (max - origin) / delta;
            if (t1 > t2) {
                var swap = t1;
                t1 = t2;
                t2 = swap;
            }
            tMin = Math.max(tMin, t1);
            tMax = Math.min(tMax, t2);
            if (tMin > tMax) return false;
        }
        return tMax > 0 && tMin < 1;
    }

    function rayIsBlocked(targetX, targetY) {
        var vx = targetX - originX;
        var vy = targetY - originY;
        var length2 = vx * vx + vy * vy;
        if (!length2) return false;

        for (var i = 0; i < activeTrees.length; i++) {
            var tree = activeTrees[i];
            if (self.externalTreeShape === "square") {
                if (segmentIntersectsSquare(targetX, targetY, tree)) return true;
                continue;
            }
            var targetDx = targetX - tree.x;
            var targetDy = targetY - tree.y;
            if (self.externalTreeBodyVisible && targetDx * targetDx + targetDy * targetDy <= tree.radius * tree.radius) {
                // The tree itself is visible. It only blocks cells after the
                // ray exits the far side of its LOS body.
                continue;
            }
            var tx = tree.x - originX;
            var ty = tree.y - originY;
            var projection = (tx * vx + ty * vy) / length2;
            if (projection <= 0 || projection >= 1) continue;

            var closestX = originX + projection * vx;
            var closestY = originY + projection * vy;
            var dx = tree.x - closestX;
            var dy = tree.y - closestY;
            if (dx * dx + dy * dy <= tree.radius * tree.radius) return true;
        }
        return false;
    }

    for (var key in this.lights) {
        if (key === this.lightOriginKey) continue;
        var target = key2pt(key);
        var visibleSample = false;
        for (var sx = 0; sx < sampleOffsets.length && !visibleSample; sx++) {
            for (var sy = 0; sy < sampleOffsets.length; sy++) {
                if (!rayIsBlocked(target.x + sampleOffsets[sx], target.y + sampleOffsets[sy])) {
                    visibleSample = true;
                    break;
                }
            }
        }
        if (!visibleSample) delete this.lights[key];
    }
}
VisionSimulation.prototype.updateVisibility = function (gX, gY, radius, continuousOrigin) {
    var self = this,
        key = xy2key(gX, gY);

    radius = radius || self.radius;
    this.lightOriginKey = key;
    this.elevation = this.elevationGrid[key].z;
    if (!this.elevationWalls[this.elevation]) this.elevationWalls[this.elevation] = generateElevationWalls(this.elevationGrid, this.elevation);
    if (!this.paddedElevationWalls[this.elevation]) this.paddedElevationWalls[this.elevation] = padBlockMap(this.elevationWalls[this.elevation], this.blockerPaddingCells);
    if (!this.elevationWallObstacles[this.elevation]) this.elevationWallObstacles[this.elevation] = blockMapToObstacleMap(this.paddedElevationWalls[this.elevation], 'elevation', Math.SQRT2 / 2);
    if (!this.clusteredTreeWalls[this.elevation]) this.clusteredTreeWalls[this.elevation] = clusterTreeObstacleMap(this.treeWalls[this.elevation], this.treeClusterMinCells, this.treeClusterPaddingCells, this.treeShadowRadiusCells);
    if (!this.paddedTreeWalls[this.elevation]) this.paddedTreeWalls[this.elevation] = padObstacleMap(this.clusteredTreeWalls[this.elevation], this.blockerPaddingCells);
    if (!this.paddedFowBlockerNode) this.paddedFowBlockerNode = padBlockMap(this.ent_fow_blocker_node, this.blockerPaddingCells);
    if (!this.fowBlockerObstacles) this.fowBlockerObstacles = blockMapToObstacleMap(this.paddedFowBlockerNode, 'fow', Math.SQRT2 / 2);
    this.activeElevationWalls = this.paddedElevationWalls[this.elevation];
    this.activeTreeWalls = this.paddedTreeWalls[this.elevation];
    this.activeFowBlockers = this.paddedFowBlockerNode;
    this.walls = mergeObstacleMaps(this.activeTreeWalls, this.elevationWallObstacles[this.elevation], this.fowBlockerObstacles);
    //setElevationWalls(this.walls, this.elevationWalls, this.elevation)
    //setWalls(this.walls, this.ent_fow_blocker_node);
    //setWalls(this.walls, this.tools_no_wards);
    //setTreeWalls(this.walls, this.elevation, this.tree, this.tree_elevations, this.tree_state, this.tree_blocks);

    this.fov.walls = this.walls;
    this.lights = {};
    this.area = this.fov.compute(gX, gY, radius, function(x2, y2, r, vis) {
        var key = xy2key(x2, y2);
        if (!self.elevationGrid[key]) return;
        if (vis == 1 && !self.activeElevationWalls[key] && !self.activeFowBlockers[key] && !(self.activeTreeWalls[key] && self.activeTreeWalls[key].length > 0)) {
            self.lights[key] = 255;
        }
    });
    this.applyExternalTreeRaycast(
        continuousOrigin ? continuousOrigin.x : gX,
        continuousOrigin ? continuousOrigin.y : gY,
        radius
    );
    this.lightArea = Object.keys(this.lights).length;
}

VisionSimulation.prototype.isValidXY = function (x, y, bCheckGridnav, bCheckToolsNoWards, bCheckTreeState) {
    if (!this.ready) return false;
    
    var key = xy2key(x, y),
        treeBlocking = false;
        
    if (bCheckTreeState) {
        var treePts = this.tree_relations[key];
        if (treePts) {
            for (var i = 0; i < treePts.length; i++) {
                var treePt = treePts[i];
                treeBlocking = this.tree_state[treePt.key];
                if (treeBlocking) break;
            }
        }
    }
    
    return x >= 0 && x < this.gridWidth && y >= 0 && y < this.gridHeight && (!bCheckGridnav || !this.gridnav[key]) && (!bCheckToolsNoWards || !this.tools_no_wards[key]) && (!bCheckTreeState || !treeBlocking);
}

VisionSimulation.prototype.toggleTree = function (x, y) {
    var self = this;
    var key = xy2key(x, y);
    var isTree = !!this.tree_relations[key];
    if (isTree) {
        var treePts = this.tree_relations[key];
        for (var i = 0; i < treePts.length; i++) {
            var pt = treePts[i];
            this.tree_state[pt.key] = !this.tree_state[pt.key];
            
            this.elevationValues.forEach(function (elevation) {
                if (elevation < self.tree_elevations[pt.key]) {
                    self.clusteredTreeWalls[elevation] = null;
                    self.paddedTreeWalls[elevation] = null;
                    self.tree_blocks[pt.key].forEach(function (ptB) {
                        for (var j = self.treeWalls[elevation][ptB.key].length - 1; j >= 0; j--) {
                            if (pt.x == self.treeWalls[elevation][ptB.key][j][1] && pt.y == self.treeWalls[elevation][ptB.key][j][2]) {
                                self.treeWalls[elevation][ptB.key].splice(j, 1);
                            }
                        }
                    });
                    if (self.tree_state[pt.key]) {
                        self.tree_blocks[pt.key].forEach(function (ptB) {
                            self.treeWalls[elevation][ptB.key] = (self.treeWalls[elevation][ptB.key] || []).concat([['tree', pt.x, pt.y, pt.shadowRadius]]);
                        });
                    }
                }
            });
        }
    }

    return isTree;
}
VisionSimulation.prototype.setRadius = function (r) {
    this.radius = r;
}
VisionSimulation.prototype.WorldXYtoGridXY = function (wX, wY, bNoRound) {
    var x = (wX - this.worldMinX) / this.cellSize,
        y = (wY - this.worldMinY) / this.cellSize;
    if (!bNoRound) {
        x = parseInt(Math.round(x))
        y = parseInt(Math.round(y))
    }
    return {x: x, y: y, key: x + ',' + y};
}
VisionSimulation.prototype.GridXYtoWorldXY = function (gX, gY) {
    return {x: gX * this.cellSize + this.worldMinX, y: gY * this.cellSize + this.worldMinY};
}

VisionSimulation.prototype.GridXYtoImageXY = function (gX, gY) {
    return {x: gX, y: this.gridHeight - gY - 1};
}

VisionSimulation.prototype.ImageXYtoGridXY = function (x, y) {
    var gY = this.gridHeight - y - 1;
    return {x: x, y: gY, key: x + ',' + gY};
}

VisionSimulation.prototype.WorldXYtoImageXY = function (wX, wY) {
    var pt = this.WorldXYtoGridXY(wX, wY);
    return this.GridXYtoImageXY(pt.x, pt.y);
}

VisionSimulation.prototype.key2pt = key2pt;
VisionSimulation.prototype.xy2key = xy2key;
VisionSimulation.prototype.xy2pt = xy2pt;
VisionSimulation.prototype.pt2key = pt2key;

module.exports = VisionSimulation;
